# -*- coding: utf-8 -*-
"""镜头质量评分（v1.4.96 废镜头筛除）：纯信号处理，零 AI 成本。

喂给画面理解/对齐的镜头如果是模糊/黑场/纯转场帧，描述会误导对齐
（主人实测的「画面不如意」根源之一）。本模块用信号处理给每个镜头
打质量分：清晰度（拉普拉斯方差）、亮度（黑场/过曝）、对比度。

评分（0-100）：
  清晰度 = 拉普拉斯方差归一（模糊检测的经典指标）
  亮度 = 均值偏离中段的惩罚
  对比度 = 标准差归一
三项加权合成，低于阈值的镜头标记为「废镜头」——
抽样跳过、对齐候选排除，省 VLM 调用且不污染对齐。
"""
import logging

import os
import numpy as np

_log = logging.getLogger('framecut.narrator')

LAPLACIAN_KERNEL = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)


def _to_gray(img):
    """RGB/BGR 数组 → 灰度（ITU-R BT.601 加权）。"""
    if img.ndim == 2:
        return img.astype(np.float64)
    return np.dot(img[..., :3].astype(np.float64), [0.299, 0.587, 0.114])


def _laplacian_var(gray):
    """拉普拉斯方差（清晰度）。手写 3x3 卷积（不引入 cv2）。"""
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    # 向量化 3x3 卷积：四邻域差分之和
    lap = (-4 * gray[1:-1, 1:-1]
           + gray[:-2, 1:-1] + gray[2:, 1:-1]
           + gray[1:-1, :-2] + gray[1:-1, 2:])
    return float(lap.var())


def frame_quality(img):
    """单帧质量分（0-100）。

    返回 (score, detail)：detail 含 clarity/brightness/contrast。"""
    gray = _to_gray(img)
    if gray.size == 0:
        return 0.0, {}
    clarity = float(min(1.0, _laplacian_var(gray) / 500.0))   # 方差 500 ≈ 文字清晰
    brightness = float(gray.mean()) / 255.0                    # 0 黑 1 白
    contrast = float(gray.std()) / 128.0                       # 0 纯色 1 高对比

    # 亮度惩罚：接近全黑（<0.06）或全白（>0.97）的重罚
    bright_pen = 1.0
    if brightness < 0.06 or brightness > 0.97:
        bright_pen = 0.3
    score = 100.0 * (0.5 * clarity + 0.3 * min(1.0, contrast) + 0.2 * min(1.0, brightness)) * bright_pen
    detail = {'clarity': round(clarity, 3), 'brightness': round(brightness, 3),
              'contrast': round(contrast, 3)}
    return round(max(0.0, min(100.0, score)), 1), detail


def shot_quality_scores(frame_paths):
    """批量：帧文件路径列表 → 质量分列表。读不了的帧给 0（废）。"""
    from PIL import Image
    scores = []
    for fp in frame_paths:
        try:
            with Image.open(fp) as im:
                img = np.asarray(im.convert('RGB'))
            sc, _ = frame_quality(img)
            scores.append(sc)
        except Exception:
            scores.append(0.0)
    return scores


def filter_good_shots(video_path, shots, ffmpeg_run_fn, quality_min=25.0,
                      progress=None, tmp_dir=None):
    """L0 镜头切分后筛除废镜头。

    每镜头抽中间 1 帧 → 质量评分（清晰度/亮度/对比度）→ 低于阈值的标记废。
    评分失败保守保留（宁滥勿缺——误杀好镜头比放过废镜头损失大）。
    返回 (good_shots, rejected_list, stats)。"""
    good, rejected = [], []
    stats = {'checked': 0, 'rejected': 0, 'errors': 0}
    import tempfile
    tmp_dir = tmp_dir or tempfile.gettempdir()
    total = len(shots or [])
    for k, (a, b) in enumerate(shots or []):
        if progress:
            progress['phase'] = '镜头质量筛查 %d/%d' % (k + 1, total)
        stats['checked'] += 1
        t = a + (b - a) * 0.5
        fp = os.path.join(tmp_dir, 'sq_%04d.jpg' % k)
        try:
            ffmpeg_run_fn(['-y', '-v', 'error', '-ss', '%.3f' % t, '-i', video_path,
                           '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '4', fp])
            if not (os.path.isfile(fp) and os.path.getsize(fp) > 1000):
                good.append((a, b))   # 抽帧失败保守保留
                continue
            from PIL import Image
            with Image.open(fp) as im:
                img = np.asarray(im.convert('L')).astype(np.float64)
            sc, _detail = frame_quality(img)
            if sc >= quality_min:
                good.append((a, b))
            else:
                rejected.append({'shot': k, 'start': a, 'end': b, 'score': sc})
                stats['rejected'] += 1
        except Exception:
            good.append((a, b))   # 评分异常保守保留
        finally:
            try:
                os.remove(fp)
            except OSError:
                pass
    _log.info('[DIAG] 废镜头筛除: %d/%d 通过（质量分 >= %.0f，废 %d）'
              % (len(good), stats['checked'], quality_min, stats['rejected']))
    return good, rejected, stats


def filter_good_shots_with_ffmpeg(video_path, shots, ffmpeg_run_fn, probe_fn,
                                  quality_min=25.0, progress=None, max_shots=220):
    """ffmpeg 直出灰度帧评分（不经 PIL，单次管道更省）。备用入口。"""
    good, rejected = [], []
    for k, (a, b) in enumerate(shots or []):
        if progress:
            progress['phase'] = '镜头质量筛查 %d/%d' % (k + 1, len(shots or []))
        t = a + (b - a) * 0.5
        try:
            import tempfile
            fd, fp = tempfile.mkstemp(prefix='sq_%04d_' % k, suffix='.png')
            os.close(fd)
            rc = ffmpeg_run_fn(['-y', '-v', 'error', '-ss', '%.3f' % t, '-i', video_path,
                                '-frames:v', '1', '-vf', 'scale=320:-2', fp])
            if rc != 0:
                good.append((a, b))   # 评分失败保守保留
                continue
            from PIL import Image
            with Image.open(fp) as im:
                img = np.asarray(im.convert('L'))
            sc, _ = frame_quality(img.astype(np.float64))
            if sc >= quality_min:
                good.append((a, b))
            else:
                rejected.append((a, b, sc))
            os.remove(fp)
        except Exception:
            good.append((a, b))
    return good, rejected
