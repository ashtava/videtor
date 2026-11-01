import subprocess, unittest
import numpy as np
from PIL import Image
import tests.test_footmap as T
from videtor.analyze import footmap

def coarse(a, size):
    return np.asarray(Image.fromarray(np.ascontiguousarray(a).astype(np.uint8)).resize(size, Image.BOX), dtype=np.float32)

class C(T.DecodeTests):
    def runTest(self):
        e = self.entries["rotated"]
        with Image.open(footmap.frame_path(self.cache, e["fid"], 2)) as im:
            ours = np.asarray(im.convert("RGB"))
        run = subprocess.run(["ffmpeg", "-v", "error", "-i", e["path"], "-vf", "fps=8:round=up:start_time=0,scale=480:854", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
        theirs = np.frombuffer(run.stdout, np.uint8).reshape(-1, 854, 480, 3)[10]
        for size in ((480, 854), (60, 107), (30, 53)):
            print('rotated', size, np.abs(coarse(ours, size) - coarse(theirs, size)).mean(), 'vs 180', np.abs(coarse(ours, size) - coarse(theirs[::-1, ::-1], size)).mean())
        e = self.entries["M2U00001"]
        with Image.open(footmap.frame_path(self.cache, e["fid"], 1)) as im:
            turned = np.asarray(im.convert("RGB"))
        with Image.open(footmap.frame_path(self.cache, self.entries["pan"]["fid"], 1)) as im:
            plain = np.asarray(im.convert("RGB"))
        back = np.rot90(turned, -1)
        wrong = np.rot90(turned, 1)
        for size in ((480, 270), (60, 34), (30, 17)):
            print('m2u', size, np.abs(coarse(back, size) - coarse(plain, size)).mean(), 'vs wrong turn', np.abs(coarse(wrong, size) - coarse(plain, size)).mean())

if __name__ == "__main__":
    s = unittest.TestSuite([C()]); unittest.TextTestRunner().run(s)
