# video-header-and-flux-compiler
after data recovery i had .mp4 header files + .mov flux files. I had to mix them to make video readable (all done with chatgpt)

I recovered files with Photorec, but all video files where split, header in a .mp4 file (something between 10-20ko) with correct infos of lenght etc..
And a file .mov that was big enought to contain all the video flux.
A soft like drilldisk was working nicelly but i didnt wanted to pay for this.

Maybe it will be usefull for somebody.


You'll need ffmpeg essential, python, and some brain cells.

Place the python script in the folder of your choice, same for ffmpeg, and files to proceed.

Here is the commande to past in a CMD. Make the modification needed 


python "C:\ **your folder where you place the python script** \auto_graft_repair.py" ^ 
  --root "C:\ ** folder to proceed**" ^
  --ffmpeg "C:\ **folder of your ffmpeg** \ffmpeg\bin\ffmpeg.exe" ^
  --ffprobe "C:\ **folder of your ffmpeg** \ffmpeg\bin\ffprobe.exe" ^
  --max-candidates 8 ^
  --escalate "8,16,30,all" 

  It will try several header to fix your video files, 
