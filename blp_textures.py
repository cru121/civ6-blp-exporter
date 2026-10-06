"""Decode Civ6 loose `CIVBIG` texture files (SHARED_DATA/TEXTURE_*) to RGB(A) numpy arrays / PNG.

Layout (verified on Babylon DLC): 48-byte header, then the top mip first (BCn block data), then smaller mips.
Formats are DXGI values from BLP::TextureEntry.m_eFormat:
  72 BC1_UNORM_SRGB, 71 BC1_UNORM, 78 BC3_UNORM_SRGB, 77 BC3_UNORM, 80 BC4_UNORM, 83 BC5_UNORM, 61 R8_UNORM
Only the top mip is decoded.
"""
import glob, os
import numpy as np

HEADER = 48
GAME = None   # set via set_game(); civ6_blp_export.py finds it automatically
_index = None


def set_game(path):
    global GAME, _index
    GAME, _index = path, None


def texture_index():
    """name -> path of every loose texture file under Base/DLC SHARED_DATA folders."""
    global _index
    if _index is None:
        _index = {}
        if not GAME:
            raise RuntimeError('game directory not set (blp_textures.set_game)')
        for pat in (os.path.join(GAME, 'Base', 'Platforms', '*', 'BLPs', 'SHARED_DATA', '*'), os.path.join(GAME, 'DLC', '*', 'Platforms', '*', 'BLPs', 'SHARED_DATA', '*')):
            for p in glob.glob(pat):
                _index.setdefault(os.path.basename(p), p)
    return _index


def _rgb565(c):
    return np.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31], 1).astype(np.int32)


def _bc1_blocks(b, alpha_mode):
    nb = len(b)
    c0 = b[:, 0].astype(np.uint16) | (b[:, 1].astype(np.uint16) << 8)
    c1 = b[:, 2].astype(np.uint16) | (b[:, 3].astype(np.uint16) << 8)
    C0, C1 = _rgb565(c0), _rgb565(c1)
    pal = np.zeros((nb, 4, 3), np.int32)
    pal[:, 0], pal[:, 1] = C0, C1
    four = (c0 > c1)[:, None] | alpha_mode        # BC3 colour blocks always use 4-colour mode
    pal[:, 2] = np.where(four, (2 * C0 + C1) // 3, (C0 + C1) // 2)
    pal[:, 3] = np.where(four, (C0 + 2 * C1) // 3, 0)
    bits = b[:, 4:8].astype(np.uint32)
    bits = bits[:, 0] | (bits[:, 1] << 8) | (bits[:, 2] << 16) | (bits[:, 3] << 24)
    idx = np.stack([((bits >> (2 * k)) & 3).astype(int) for k in range(16)], 1)      # (nb,16)
    return pal[np.arange(nb)[:, None], idx]                                           # (nb,16,3)


def _bc4_blocks(b):
    nb = len(b)
    a0 = b[:, 0].astype(np.int32); a1 = b[:, 1].astype(np.int32)
    pal = np.zeros((nb, 8), np.int32); pal[:, 0], pal[:, 1] = a0, a1
    for k in range(1, 7):
        pal[:, k + 1] = np.where(a0 > a1, ((7 - k) * a0 + k * a1) // 7, np.where(k <= 4, ((5 - k) * a0 + k * a1) // 5, 0))
    pal[:, 6] = np.where(a0 > a1, pal[:, 6], 0)
    pal[:, 7] = np.where(a0 > a1, pal[:, 7], 255)
    bits = np.zeros(nb, np.uint64)
    for k in range(6):
        bits |= b[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
    idx = np.stack([((bits >> np.uint64(3 * k)) & np.uint64(7)).astype(int) for k in range(16)], 1)
    return pal[np.arange(nb)[:, None], idx]                                           # (nb,16)


def _tile(blocks16, w, h):
    """(nb,16[,c]) -> (h,w[,c]) image."""
    c = blocks16.shape[2:] if blocks16.ndim == 3 else ()
    img = blocks16.reshape((h // 4, w // 4, 4, 4) + c)
    return img.transpose((0, 2, 1, 3) + tuple(range(4, 4 + len(c)))).reshape((h, w) + c)


def decode(data, fmt, w, h):
    """Return uint8 array (h,w,3|4|1) of the top mip."""
    body = np.frombuffer(data[HEADER:], np.uint8)
    nb = (w // 4) * (h // 4)
    if fmt in (71, 72):
        return _tile(_bc1_blocks(body[:nb * 8].reshape(nb, 8), False), w, h).astype(np.uint8)
    if fmt in (77, 78):
        blk = body[:nb * 16].reshape(nb, 16)
        rgb = _bc1_blocks(blk[:, 8:], True)
        # BC3 alpha block is BC4
        a = _bc4_blocks(blk[:, :8])
        return _tile(np.concatenate([rgb, a[:, :, None]], 2), w, h).astype(np.uint8)
    if fmt == 80:
        return _tile(_bc4_blocks(body[:nb * 8].reshape(nb, 8)), w, h).astype(np.uint8)[:, :, None]
    if fmt == 83:
        blk = body[:nb * 16].reshape(nb, 16)
        r, g = _bc4_blocks(blk[:, :8]), _bc4_blocks(blk[:, 8:])
        return _tile(np.stack([r, g], 2), w, h).astype(np.uint8)
    if fmt == 61:
        return body[:w * h].reshape(h, w, 1).copy()
    raise ValueError('unsupported DXGI format %d' % fmt)


def to_png(arr, path, kind='color'):
    from PIL import Image
    if arr.shape[2] == 1:
        Image.fromarray(arr[:, :, 0], 'L').save(path)
    elif arr.shape[2] == 2:        # BC5 two-channel -> RGB with reconstructed Z (tangent-space normal)
        x = arr[:, :, 0] / 127.5 - 1; y = arr[:, :, 1] / 127.5 - 1
        z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
        rgb = np.stack([arr[:, :, 0], arr[:, :, 1], np.clip((z * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)], 2)
        Image.fromarray(rgb, 'RGB').save(path)
    else:
        Image.fromarray(arr, 'RGBA' if arr.shape[2] == 4 else 'RGB').save(path)
