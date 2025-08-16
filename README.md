# wayland-clip-sync
Quick and dirty server + client for syncing wayland clipboards cross machines.


# USE:
I reccomend setting up an ssh tunnel, then handling the python on your localhost.
This is how I use it:


## SERVER:
wayland-clip-sync.py --listen 127.0.0.1:8377 --interval 0.10

## CLIENT:
wayland-clip-sync.py --connect 127.0.0.1:8377 --interval 0.10