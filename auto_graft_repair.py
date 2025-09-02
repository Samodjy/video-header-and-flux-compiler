# auto_graft_repair.py
# Greffe auto moov(.mp4) + mdat(.mov) avec patch offsets, test, audio, escalade de headers, et archivage des MOV OK.

import os, sys, csv, re, struct, shutil, argparse, subprocess
from pathlib import Path

# ---------- FFPROBE / FFMPEG helpers ----------
def ffprobe_streams(ffprobe, path):
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
            stderr=subprocess.STDOUT, text=True
        )
        info = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        return info
    except subprocess.CalledProcessError:
        return {}

def header_signature(ffprobe, path: Path) -> str:
    info = ffprobe_streams(ffprobe, path)
    codec = info.get("codec_name", "?")
    w = info.get("width", "?")
    h = info.get("height", "?")
    r = info.get("avg_frame_rate", "?")
    return f"{codec}-{w}x{h}-{r}"

def test_decode(ffmpeg, video_path: Path, out_dir: Path) -> bool:
    out_jpg = out_dir / (video_path.stem + "_probe.jpg")
    if out_jpg.exists():
        try: out_jpg.unlink()
        except: pass
    cmd = [ffmpeg, "-hide_banner", "-y", "-v", "error",
           "-i", str(video_path), "-frames:v", "1", str(out_jpg)]
    rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ok = (rc.returncode == 0 and out_jpg.exists())
    if not ok and out_jpg.exists():
        try: out_jpg.unlink()
        except: pass
    return ok

def mux_audio(ffmpeg, fixed_video: Path, data_src: Path, out_final: Path) -> bool:
    """
    Tente d'ajouter l'audio du DATA (.mov) à la vidéo réparée (fixed_video),
    sans ré-encoder. Si échec, out_final n'est pas créé.
    """
    tmp = out_final.with_suffix(".tmp.mp4")
    cmd = [ffmpeg, "-hide_banner", "-y",
           "-i", str(fixed_video), "-i", str(data_src),
           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(tmp)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode == 0 and tmp.exists():
        tmp.replace(out_final)
        return True
    if tmp.exists():
        try: tmp.unlink()
        except: pass
    return False

# ---------- MP4 box parsing & patch ----------
def read_box(f, at=None):
    if at is not None:
        f.seek(at)
    hdr = f.read(8)
    if len(hdr) < 8:
        return None, None, None, None
    size, boxtype = struct.unpack(">I4s", hdr)
    header_len = 8
    if size == 1:
        largesz = f.read(8)
        if len(largesz) < 8:
            return None, None, None, None
        size = struct.unpack(">Q", largesz)[0]
        header_len = 16
    return boxtype, size, f.tell() - header_len, header_len  # type, total, start, hdr_len

def find_top_boxes(f):
    out = {}
    f.seek(0, os.SEEK_END)
    end = f.tell()
    pos = 0
    f.seek(0)
    while pos + 8 <= end:
        t, sz, start, hdr = read_box(f, pos)
        if not t or sz < hdr or start + sz > end or sz == 0:
            break
        out.setdefault(t, []).append((start, sz, hdr))
        pos = start + sz
    return out

def walk_children(f, parent_start, parent_size, parent_hdr_len, callback):
    end = parent_start + parent_size
    pos = parent_start + parent_hdr_len
    while pos + 8 <= end:
        t, sz, s, hdr = read_box(f, pos)
        if not t or sz < hdr or s + sz > end or sz == 0:
            break
        callback(t, s, sz, hdr)
        new_pos = s + sz
        if new_pos <= pos:
            break
        pos = new_pos

def patch_stco_tables(in_path: Path, out_path: Path) -> int:
    """
    Ouvre in_path, calcule delta = (debut charge mdat) - (min(stco/co64)),
    puis écrit une copie patchée dans out_path. Retourne delta appliqué.
    """
    with open(in_path, "rb") as f:
        tops = find_top_boxes(f)
        if b"moov" not in tops: raise RuntimeError("moov not found")
        if b"mdat" not in tops: raise RuntimeError("mdat not found")

        mdat_start, mdat_size, mdat_hdr = tops[b"mdat"][0]
        mdat_payload = mdat_start + mdat_hdr

        moov_start, moov_size, moov_hdr = tops[b"moov"][0]
        st_positions = []  # (entries_offset, count, bits)
        min_offset = None

        def visit(t, s, sz, hdr):
            nonlocal min_offset
            if t in (b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"):
                walk_children(f, s, sz, hdr, visit)
                return
            if t == b"stco":
                f.seek(s + hdr); f.read(4)  # version+flags
                count = struct.unpack(">I", f.read(4))[0]
                entries_off = f.tell()
                for _ in range(count):
                    off = struct.unpack(">I", f.read(4))[0]
                    if min_offset is None or off < min_offset:
                        min_offset = off
                st_positions.append((entries_off, count, 32))
            elif t == b"co64":
                f.seek(s + hdr); f.read(4)
                count = struct.unpack(">I", f.read(4))[0]
                entries_off = f.tell()
                for _ in range(count):
                    off = struct.unpack(">Q", f.read(8))[0]
                    if min_offset is None or off < min_offset:
                        min_offset = off
                st_positions.append((entries_off, count, 64))

        walk_children(f, moov_start, moov_size, moov_hdr, visit)

        if min_offset is None:
            raise RuntimeError("No stco/co64 found")

        delta = mdat_payload - min_offset

    shutil.copyfile(in_path, out_path)
    if delta == 0:
        return delta

    with open(out_path, "r+b") as f:
        for entries_off, count, bits in st_positions:
            f.seek(entries_off)
            if bits == 64:
                for _ in range(count):
                    off = struct.unpack(">Q", f.read(8))[0]
                    f.seek(-8, os.SEEK_CUR)
                    f.write(struct.pack(">Q", off + delta))
            else:
                for _ in range(count):
                    off = struct.unpack(">I", f.read(4))[0]
                    f.seek(-4, os.SEEK_CUR)
                    f.write(struct.pack(">I", off + delta))
    return delta

# ---------- pairing logic ----------
FNUM_RE = re.compile(r"f(\d+)")

def fnum(path: Path):
    m = FNUM_RE.match(path.stem)
    return int(m.group(1)) if m else 10**12  # gros nombre si pas de f#######

def is_header(p: Path) -> bool:
    name = p.stem.lower()
    return (p.suffix.lower() == ".mp4") and ("_mdat" not in name) and ("_free" not in name)

def is_data(p: Path) -> bool:
    name = p.stem.lower()
    return (p.suffix.lower() == ".mov") and (("_mdat" in name) or ("_free" in name))

def concat_files(header: Path, data: Path, out_path: Path):
    with open(out_path, "wb") as w, open(header, "rb") as h, open(data, "rb") as d:
        shutil.copyfileobj(h, w)
        shutil.copyfileobj(d, w)

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Greffe auto moov(.mp4) + mdat(.mov) avec patch offsets, audio, escalade, et archivage MOV OK")
    ap.add_argument("--root", required=True, help="Dossier contenant les f########.mp4/.mov")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="Chemin vers ffmpeg.exe")
    ap.add_argument("--ffprobe", default="ffprobe", help="Chemin vers ffprobe.exe")
    ap.add_argument("--max-candidates", type=int, default=8, help="Taille de base (palier initial) pour les essais")
    ap.add_argument("--escalate", default="8,16,30,all",
                    help="Paliers d’essai successifs, ex: '6,12,all'. 'all' = tous les headers")
    ap.add_argument("--archive-mov", action="store_true", default=True, help="Déplacer le MOV dès qu'une paire est OK (par défaut: on)")
    ap.add_argument("--no-archive-mov", action="store_false", dest="archive_mov", help="Ne pas déplacer les MOV OK")
    args = ap.parse_args()

    root = Path(args.root)
    ffmpeg = args.ffmpeg
    ffprobe = args.ffprobe

    out_root = root / "OUT_graft"
    out_ok   = out_root / "ok"
    out_try  = out_root / "try"
    out_done = out_root / "mov_ok"  # MOV sources déplacés lorsqu'OK
    for p in (out_root, out_ok, out_try, out_done):
        p.mkdir(parents=True, exist_ok=True)
    out_csv = out_root / "report.csv"

    # Récupère listes
    files = sorted([p for p in root.glob("*.*") if p.is_file()])
    headers = [p for p in files if is_header(p)]
    datas   = [p for p in files if is_data(p) and p.stat().st_size > 0]

    if not headers:
        print("Aucun header .mp4 détecté.")
        sys.exit(1)
    if not datas:
        print("Aucun DATA *_mdat/_free .mov (non vide) détecté.")
        sys.exit(1)

    # Signatures de headers
    sig_map = {}
    for h in headers:
        sig = header_signature(ffprobe, h)
        sig_map.setdefault(sig, []).append(h)

    headers_sorted = sorted(headers, key=lambda p: (fnum(p), p.name))

    # Préparer niveaux d'escalade
    levels = []
    for tok in (args.escalate or "").split(","):
        tok = tok.strip().lower()
        if not tok: continue
        if tok == "all":
            levels.append(len(headers_sorted))
        else:
            try:
                levels.append(max(1, int(tok)))
            except:
                pass
    if not levels:
        levels = [args.max_candidates, min(len(headers_sorted), max(args.max_candidates*2, 16)), len(headers_sorted)]

    with open(out_csv, "w", newline="", encoding="utf-8") as rfp:
        wcsv = csv.writer(rfp, delimiter=";")
        wcsv.writerow(["data","data_size","header","header_sig","delta_added","status","output"])

        for d in datas:
            data_size = d.stat().st_size
            dn = fnum(d)
            print(f"\n[DATA] {d.name}  ({data_size/1_000_000:.1f} MB)")

            # ordre de proximité
            h_by_dist = sorted(headers_sorted, key=lambda h: abs(dn - fnum(h)))
            success = False

            for cap in levels:
                # 1) base: proches (cap//2)
                seen = set()
                candidates = []
                nprox = max(1, cap // 2)
                for h in h_by_dist[:nprox]:
                    if h not in seen:
                        candidates.append(h); seen.add(h)

                # 2) un header par signature (compléter jusqu'à cap)
                for sig, lst in sig_map.items():
                    h = sorted(lst, key=lambda p: abs(dn - fnum(p)))[0]
                    if h not in seen:
                        candidates.append(h); seen.add(h)
                        if len(candidates) >= cap: break

                # 3) si pas assez, compléter par plus proches restants
                if len(candidates) < cap:
                    for h in h_by_dist:
                        if h not in seen:
                            candidates.append(h); seen.add(h)
                            if len(candidates) >= cap: break

                print(f"  -> essaie {len(candidates)} headers (palier {cap})")

                # ===== essais du palier =====
                for h in candidates:
                    sig = header_signature(ffprobe, h)
                    combo = out_try / f"{h.stem}__{d.stem}.mp4"
                    fixed = out_try / f"{h.stem}__{d.stem}_fixed.mp4"

                    # 1) concat
                    try:
                        concat_files(h, d, combo)
                    except Exception as e:
                        print(f"  - {h.name}: concat FAIL: {e}")
                        wcsv.writerow([d.name, data_size, h.name, sig, "", "concat_fail", str(combo)])
                        continue

                    # 2) patch
                    try:
                        delta = patch_stco_tables(combo, fixed)
                    except Exception as e:
                        print(f"  - {h.name}: patch FAIL: {e}")
                        wcsv.writerow([d.name, data_size, h.name, sig, "", "patch_fail", str(combo)])
                        continue

                    # 3) test decode
                    ok = test_decode(ffmpeg, fixed, out_try)
                    if ok:
                        out_final = out_ok / f"{h.stem}__{d.stem}.mp4"

                        # a) audio du MOV (sans ré-encoder) si possible
                        if not mux_audio(ffmpeg, fixed, d, out_final):
                            # b) remux propre si pas d'audio ajouté
                            tmp_final = out_ok / f"{h.stem}__{d.stem}_tmp.mp4"
                            subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(fixed), "-c", "copy", str(tmp_final)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            if tmp_final.exists():
                                tmp_final.replace(out_final)
                            else:
                                shutil.copy2(fixed, out_final)

                        print(f"  + OK avec {h.name} ({sig}) -> {out_final.name}")
                        wcsv.writerow([d.name, data_size, h.name, sig, delta, "OK", str(out_final)])

                        # déplacer le MOV source traité
                        if args.archive_mov:
                            pair_dir = out_done / f"{h.stem}__{d.stem}"
                            pair_dir.mkdir(parents=True, exist_ok=True)
                            try:
                                shutil.move(str(d), str(pair_dir / d.name))
                            except Exception as e:
                                print(f"    [WARN] move data failed {d}: {e}")
                            # (option) trace du header :
                            # shutil.copy2(str(h), str(pair_dir / h.name))

                        success = True
                        break
                    else:
                        print(f"  - {h.name}: decode FAIL")
                        wcsv.writerow([d.name, data_size, h.name, sig, delta, "decode_fail", str(fixed)])

                if success:
                    break  # stop escalade pour ce DATA

            if not success:
                print("  => Aucun header concluant pour ce DATA.")

    print(f"\nFini. Regarde: {out_root}\n"
          f" - ok\\        (vidéos réparées)\n"
          f" - try\\       (intermédiaires + _probe.jpg)\n"
          f" - mov_ok\\    (MOV sources déplacés quand OK)\n"
          f" - report.csv (récapitulatif)\n")

if __name__ == "__main__":
    main()
