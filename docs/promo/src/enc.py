import glob, sys, time
from PIL import Image
q, out = int(sys.argv[1]), sys.argv[2]
files = sorted(glob.glob('f720/*.png'))
ims = [Image.open(f) for f in files]
# 24 fps = 41.67 ms per frame; alternate 42/42/41 so the total stays exact.
dur = [41 if i % 3 == 2 else 42 for i in range(len(ims))]
t = time.time()
ims[0].save(out, save_all=True, append_images=ims[1:], duration=dur, loop=0, quality=q, method=4, kmin=5, kmax=10, minimize_size=False, allow_mixed=False)
print(out, len(files), round(time.time() - t), 's')
