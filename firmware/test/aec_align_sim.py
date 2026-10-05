"""RealtimeAEC 自动延迟对齐 (auto_align) 算法仿真验证

主固件 AEC 后端已切换为 esp-sr 官方 esp_aec (方案 A)。
本脚本 1:1 移植 firmware/src/llm/RealtimeAEC.cpp 的参考对齐逻辑
(esp_aec 要求 ref 与 mic 严格对齐, 对齐由 auto_align 互相关完成):
  - 合成"类语音"播放信号, 麦克风 = 延迟 L 的回波 + 噪声 (+ 间歇性人声干扰)
  - 跑粗搜互相关累加 / 峰均比阈值 / 精搜逻辑
  - 验证 _k0 收敛到真实延迟 L (误差 ≤ 精搜范围)
  - 附: NLMS 回退后端端到端消回声对照 (验证对齐有效性)

运行: python3 firmware/test/aec_align_sim.py
"""
import math
import random

random.seed(42)

FRAME = 256          # 16ms @16kHz (esp_aec 帧长, 与主固件一致)
TAPS = 256           # NLMS 后端回声尾 (与主固件 AEC_FILTER_TAPS 一致)
ESP_FILTER = 1200    # esp_aec 回声尾 (aec_create filter)
HIST = 4096
STEP = 16
BINS = (HIST - ESP_FILTER) // STEP   # 粗搜 bins
EVERY = 25

ref = [0] * HIST
pos = 0
align_acc = [0.0] * BINS
align_cnt = 0
last_frame = [0] * FRAME
k0 = 0
aligned = False
updates = []


def push(samples):
    global pos
    for s in samples:
        ref[pos] = s
        pos = (pos + 1) % HIST


def corr_accum(frame):
    global align_cnt, k0, aligned
    base = pos - FRAME
    if base < 0:
        base += HIST
    for b in range(BINS):
        idx = base - b * STEP
        acc = align_acc[b]
        for n in range(0, FRAME, 2):
            i2 = idx + n
            if i2 < 0:
                i2 += HIST
            elif i2 >= HIST:
                i2 -= HIST
            acc += frame[n] * ref[i2]
        align_acc[b] = acc
    last_frame[:] = frame
    align_cnt += 1
    if align_cnt >= EVERY:
        update_delay()
        align_cnt = 0
        align_acc[:] = [0.0] * BINS


pending = -1


def update_delay():
    global k0, aligned, pending
    best = 0.0
    best_b = 0
    mean = 0.0
    for b in range(BINS):
        v = abs(align_acc[b])
        mean += v
        if v > best:
            best, best_b = v, b
    mean /= BINS
    if mean < 1e-6 or best < mean * 1.6:
        return
    base = pos - FRAME
    if base < 0:
        base += HIST
    center = best_b * STEP
    hi_max = HIST - TAPS          # NLMS 后端上限 (与 C++ 一致)
    lo = max(0, center - STEP)
    hi = min(center + STEP, hi_max)
    best_f = 0.0
    best_lag = center
    for lag in range(lo, hi + 1):
        idx = base - lag
        acc = 0.0
        for n in range(0, FRAME, 2):
            i2 = idx + n
            if i2 < 0:
                i2 += HIST
            elif i2 >= HIST:
                i2 -= HIST
            acc += last_frame[n] * ref[i2]
        if acc > best_f:
            best_f, best_lag = acc, lag
    new_k0 = min(best_lag, hi_max)

    # 稳定性策略 (与 C++ _updateDelay 一致):
    #   小变化 (≤64) EMA 平滑; 显著变化需连续两次一致 (±32) 才接受
    if not aligned:
        k0 = new_k0
        aligned = True
        pending = -1
    else:
        diff = new_k0 - k0
        if -64 <= diff <= 64:
            k0 = (3 * k0 + new_k0) // 4
            pending = -1
        else:
            if pending >= 0 and abs(new_k0 - pending) <= 32:
                k0 = new_k0
                pending = -1
            else:
                pending = new_k0
    updates.append(k0)


def speech_sample(t, i):
    """类语音: 基频漂移多谐波 + 音节包络 (与 bench 信号同构)"""
    tt = (t + i) / 16000.0
    f0 = 180.0 + 25.0 * math.sin(2 * math.pi * 0.7 * tt)
    s = sum(math.sin(2 * math.pi * f0 * h * tt) / h for h in range(1, 9))
    ph = (t + i) % 960
    env = (0.5 - 0.5 * math.cos(2 * math.pi * ph / 720.0)) if ph < 720 else 0.0
    return int(s * env * 4000.0)


def sim(name, L, gain, noise_rms, human_every=0, frames=500):
    global ref, pos, align_acc, align_cnt, last_frame, k0, aligned, updates, pending
    ref = [0] * HIST
    pos = 0
    align_acc = [0.0] * BINS
    align_cnt = 0
    last_frame = [0] * FRAME
    k0 = 0
    aligned = False
    pending = -1
    updates = []

    all_play = []      # 全局播放序列 (供任意延迟回波合成)
    t = 0
    for f in range(frames):
        play = [speech_sample(t, i) for i in range(FRAME)]
        all_play.extend(play)
        # mic[n] = play[t-L+n] (时刻 t+n 听到 t+n-L 的播放); t-L<0 处回声未到达取 0
        mic = [((all_play[t - L + n] if t - L + n >= 0 else 0) * gain
                + random.uniform(-noise_rms, noise_rms))
               for n in range(FRAME)]
        if human_every and f % human_every == 0:
            for i in range(FRAME):
                mic[i] += random.uniform(-1, 1) * 2500.0   # 近端人声干扰
        push(play)
        corr_accum(mic)
        t += FRAME
    print(f"{name}: L={L} gain={gain} noise={noise_rms:.0f} human_every={human_every}")
    print(f"  k0 更新序列: {updates}")
    final = k0
    ok = abs(final - L) <= STEP
    print(f"  最终 k0={final} (真实 {L}) → {'PASS' if ok else 'FAIL'}")
    return ok


all_ok = True
all_ok &= sim("基准 50ms", 800, 0.35, 5)
all_ok &= sim("长延迟 128ms", 2048, 0.35, 5)
all_ok &= sim("短延迟 12ms", 192, 0.35, 5)
all_ok &= sim("人声干扰 50ms", 800, 0.35, 5, human_every=8)
all_ok &= sim("弱回声 50ms", 800, 0.12, 30)
all_ok &= sim("大噪声 50ms", 800, 0.35, 200)

# =============================================================================
# 端到端: auto_align 估计的 k0 + NLMS 回声消除 (对比无对齐 k0=0)
# =============================================================================

_wide_phases = [random.uniform(0, 6.283) for _ in range(16)]


def speech_sample_wide(t, i):
    """宽带类语音: 带包络的宽带噪声 (连续谱; 延迟只能靠正确的参考位置对齐,
    无法被零延迟 FIR 频域拟合 — 用于判别 k0 对齐必要性; e2e 用) """
    tt = t + i
    n = random.uniform(-1.0, 1.0)
    g = 0.35 + 0.65 * abs(math.sin(2 * math.pi * tt / 12800.0))
    return int(n * g * 3500)


def e2e(k0_mode, L=800, gain=0.35, frames=500):
    """k0_mode: 'none' (k0=0) / 'auto' (auto_align 估计)。
    返回 (输入RMS, 残余RMS)。"""
    global ref, pos, align_acc, align_cnt, last_frame, k0, aligned, updates, pending
    ref = [0] * HIST
    pos = 0
    align_acc = [0.0] * BINS
    align_cnt = 0
    last_frame = [0] * FRAME
    k0 = 0
    aligned = False
    pending = -1
    updates = []
    w = [0.0] * TAPS

    all_play = []
    t = 0
    in2 = out2 = 0.0
    counted = 0
    for f in range(frames):
        play = [speech_sample_wide(t, i) for i in range(FRAME)]
        all_play.extend(play)
        mic = [((all_play[t - L + n] if t - L + n >= 0 else 0) * gain
                + random.uniform(-5, 5))
               for n in range(FRAME)]
        push(play)
        if k0_mode == 'auto':
            corr_accum(list(mic))
        kk = k0 if k0_mode == 'auto' else 0

        # NLMS 滤波 (与 RealtimeAEC::_processNlms 同构): 参考随样本滑动 +
        # 滑窗能量 = 初窗全量 + 帧内增量 O(1)
        base_ref = pos - FRAME - kk
        if base_ref < 0:
            base_ref += HIST
        pow_n = 0.0
        for k in range(TAPS):
            xk = ref[(base_ref - k) % HIST]
            pow_n += xk * xk
        for n in range(FRAME):
            y = 0.0
            for k in range(TAPS):
                y += w[k] * ref[(base_ref + n - k) % HIST]
            e = mic[n] - y
            if pow_n > 1.0:      # 静音保护: 参考能量过低时不更新
                step_n = 0.1 / (pow_n + 1e-6)
                for k in range(TAPS):
                    w[k] += step_n * e * ref[(base_ref + n - k) % HIST]
            if n + 1 < FRAME:    # 增量更新窗能量
                xa = ref[(base_ref + n + 1) % HIST]
                xs = ref[(base_ref + n - (TAPS - 1)) % HIST]
                pow_n += xa * xa - xs * xs
            if f >= 250:      # 收敛后统计
                in2 += mic[n] * mic[n]
                out2 += e * e
                counted += 1
        if f == 300:          # 调试: 看权重与残差状态
            y0 = sum(w[k] * ref[(base_ref + 0 - k) % HIST] for k in range(TAPS))
            print(f"    [dbg] f={f} kk={kk} k0={k0} "
                  f"max|w|={max(abs(t) for t in w):.2e} y0={y0:.0f} e0={mic[0]-y0:.0f} mic0={mic[0]:.0f}")
        t += FRAME
    in_rms = math.sqrt(in2 / counted)
    out_rms = math.sqrt(out2 / counted)
    return in_rms, out_rms

print("== 端到端 (NLMS 消回声, 延迟 800 样本/50ms) ==")
in_r, out_none = e2e('none')
in_r, out_auto = e2e('auto')
print(f"  输入 RMS={in_r:.0f}")
print(f"  无对齐 k0=0    → 残余 RMS={out_none:.0f} (基本无消除)")
print(f"  auto_align k0  → 残余 RMS={out_auto:.0f}")
print(f"  残余下降: {20*math.log10(out_auto/out_none):.1f} dB (相对无对齐基线)")
print(f"  相对输入衰减: {20*math.log10(out_auto/in_r):.1f} dB (真正验证: 应显著为负)")
ok2 = out_auto < in_r * 0.4   # 残余须比输入低 8dB 以上 (相对输入验证, 不再只看发散基线)
print(f"  → {'PASS' if ok2 else 'FAIL'}")
all_ok &= ok2

print(f"\n{'ALL PASS' if all_ok else 'SOME FAILED'}")
raise SystemExit(0 if all_ok else 1)
