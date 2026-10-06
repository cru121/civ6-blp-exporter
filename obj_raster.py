"""Textured preview of an exported OBJ (+MTL map_Kd): python obj_raster.py model.obj out.png [size] [azimuth]"""
import sys, os, numpy as np
from PIL import Image
def load(fn):
    d = os.path.dirname(fn); V, UV, mats, F = [], [], {}, []
    cur = None
    for ln in open(fn):
        t = ln.split()
        if not t: continue
        if t[0] == 'v': V.append(list(map(float, t[1:4])))
        elif t[0] == 'vt': UV.append(list(map(float, t[1:3])))
        elif t[0] == 'mtllib':
            m = None
            for l2 in open(os.path.join(d, t[1])):
                u = l2.split()
                if u and u[0] == 'newmtl': m = u[1]
                elif u and u[0] == 'map_Kd' and m: mats[m] = np.array(Image.open(os.path.join(d, u[1])).convert('RGB'))
        elif t[0] == 'usemtl': cur = t[1]
        elif t[0] == 'f': F.append((cur, [int(x.split('/')[0]) - 1 for x in t[1:4]]))
    return np.array(V), np.array(UV), mats, F
def render(fn, W=700, az=35, el=30):
    V, UV, mats, F = load(fn); img = np.zeros((W, W, 3), np.uint8) + 30; zb = np.full((W, W), 1e9)
    az, el = np.radians(az), np.radians(el)
    R = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]]); Q = V @ R.T
    sx = Q[:, 0]; sy = Q[:, 2] * np.cos(el) + Q[:, 1] * np.sin(el); dp = Q[:, 1] * np.cos(el) - Q[:, 2] * np.sin(el)
    sc = W / (max(np.ptp(sx), np.ptp(sy)) * 1.1); X = (sx - (sx.max() + sx.min()) / 2) * sc + W / 2; Y = W / 2 - (sy - (sy.max() + sy.min()) / 2) * sc
    for mat, t in F:
        T = mats.get(mat, np.full((4, 4, 3), 128, np.uint8)); th, tw = T.shape[:2]
        xs, ys, ds = X[t], Y[t], dp[t]; uv = UV[t]
        x0, x1 = int(max(xs.min(), 0)), int(min(xs.max() + 1, W - 1)); y0, y1 = int(max(ys.min(), 0)), int(min(ys.max() + 1, W - 1))
        den = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if abs(den) < 1e-9 or x1 < x0 or y1 < y0: continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + .5, np.arange(y0, y1 + 1) + .5)
        l0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / den
        l1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / den
        l2 = 1 - l0 - l1; m = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
        if not m.any(): continue
        z = l0 * ds[0] + l1 * ds[1] + l2 * ds[2]
        u = l0 * uv[0, 0] + l1 * uv[1, 0] + l2 * uv[2, 0]; v = 1 - (l0 * uv[0, 1] + l1 * uv[1, 1] + l2 * uv[2, 1])   # undo OBJ 1-v
        col = T[np.floor((v % 1) * th).astype(int).clip(0, th - 1), np.floor((u % 1) * tw).astype(int).clip(0, tw - 1)]
        sub = zb[y0:y1 + 1, x0:x1 + 1]; ok = m & (z < sub); sub[ok] = z[ok]; img[y0:y1 + 1, x0:x1 + 1][ok] = col[ok]
    return img
if __name__ == '__main__':
    Image.fromarray(render(sys.argv[1], int(sys.argv[3]) if len(sys.argv) > 3 else 700, float(sys.argv[4]) if len(sys.argv) > 4 else 35)).save(sys.argv[2])
