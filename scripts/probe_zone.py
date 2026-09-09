"""Точечная проверка зоны сдвига: PyAV seek vs cv2 seek vs эталон.

Эталон = cv2 последовательное чтение, начиная с 74000 (зона до сдвига,
где cv2 все корректен). Затем сравниваем выборочные кадры, полученные:
  - PyAV seek (container.seek по PTS + декод до цели)
  - cv2 seek (cap.set POS_FRAMES + read)
побитово с эталоном.

Зона сдвига у Alyosha: с 74174.
"""
import cv2
import hashlib
import av

P = "/d/Work/AD/DataOps/Боевые видео/H/Alyosha.mp4 [-144016306_456239404].mp4"
START = 74000
TAIL = 74300

def dg(img):
    return hashlib.sha256(img.tobytes()).hexdigest()[:16]

# 1) Эталон: cv2 sequential, read-only начиная с START.
ref = {}
cap = cv2.VideoCapture(P)
cap.set(cv2.CAP_PROP_POS_FRAMES, START)
for i in range(START, TAIL):
    ok, f = cap.read()
    if not ok:
        break
    ref[i] = dg(f)
cap.release()
print("эталон построен:", len(ref), "кадров")

# 2) Точность эталона: повторный независимый sequential.
cap2 = cv2.VideoCapture(P)
cap2.set(cv2.CAP_PROP_POS_FRAMES, START)
bad_ref = 0
for i in range(START, TAIL):
    ok, f = cap2.read()
    if ok and dg(f) != ref.get(i):
        bad_ref += 1
cap2.release()
print("эталон самосогласован:", "да" if bad_ref == 0 else f"НЕТ ({bad_ref})")

# 3) Проверка seek выборочно.
probe = list(range(START + 150, TAIL, 5))  # 74150..74295, каждый 5-й

# PyAV seek
container = av.open(P)
stream = container.streams.video[0]
tb = stream.time_base

# Строим точное отображение idx -> PTS по секвенциальному проходу PyAV
# (начать с 0, читать до TAIL). Это даёт честные времена даже на VFR.
pts_map = {}
dig_seq = {}
c2 = av.open(P)
s2 = c2.streams.video[0]
n = 0
for frame in c2.decode(s2):
    if n >= TAIL:
        break
    pts_map[n] = (frame.pts or 0) * (frame.time_base or tb)
    if n >= START:
        dig_seq[n] = hashlib.sha256(frame.to_ndarray(format="bgr24").tobytes()).hexdigest()[:16]
    n += 1
c2.close()
print("PyAV sequential pts построены:", len(pts_map))

pyav_bad = []
for i in probe:
    target_s = pts_map[i]
    offset = int(target_s / tb) - 1
    container.seek(offset, stream=stream, backward=True, any_frame=False)
    got = None
    for frame in container.decode(stream):
        f_s = (frame.pts or 0) * (frame.time_base or tb)
        if f_s >= target_s:
            got = hashlib.sha256(frame.to_ndarray(format="bgr24").tobytes()).hexdigest()[:16]
            break
    if got != dig_seq.get(i):
        kind = "?"
        if got == dig_seq.get(i - 1):
            kind = "i-1"
        elif got == dig_seq.get(i + 1):
            kind = "i+1"
        pyav_bad.append((i, kind))
container.close()
print("PyAV seek (по точным PTS): bad", len(pyav_bad), pyav_bad[:8])

# cv2 seek, сверка с PyAV-sequential-эталоном (а не с cv2-эталоном!)
cap3 = cv2.VideoCapture(P)
cv_bad = []
for i in probe:
    cap3.set(cv2.CAP_PROP_POS_FRAMES, i)
    ok, f = cap3.read()
    got = dg(f) if ok else None
    if got != dig_seq.get(i):
        kind = "?"
        if got == dig_seq.get(i - 1):
            kind = "i-1"
        elif got == dig_seq.get(i + 1):
            kind = "i+1"
        cv_bad.append((i, kind))
cap3.release()
print("cv2 seek (vs PyAV-эталон): bad", len(cv_bad), cv_bad[:8])