#!/usr/bin/env python3
"""Repeatable local film-dialogue extraction. See references/commands.md."""
import argparse
import csv
import filecmp
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

REVISION = 1
MODEL = 'UVR-MDX-NET-Inst_HQ_3.onnx'


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def file_id(path):
    p = Path(path).resolve(strict=True)
    s = p.stat()
    return {'path': str(p), 'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ff(args):
    subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin', *map(str, args)], check=True)


def runtime(args):
    return Path(args.runtime).expanduser().resolve()


def safe_name(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('角色名和素材ID须为非空字符串，前后不留空格')
    if re.search(r'[/\\\x00-\x1f:*?"<>|]', value) or value in ('.', '..'):
        raise ValueError(f'不能用于文件名: {value!r}')
    return value


def probe(path):
    import av
    with av.open(str(path)) as c:
        audio = list(c.streams.audio)
        if not audio:
            raise ValueError(f'没有音轨: {path}')
        duration = c.duration / av.time_base if c.duration is not None else None
        if duration is None:
            durations = [float(s.duration * s.time_base) for s in audio if s.duration is not None]
            duration = max(durations) if durations else None
        return {'path': str(Path(path).resolve()), 'duration_seconds': duration,
                'audio_streams': [{'audio_stream': i, 'codec': s.codec_context.name,
                                   'sample_rate': s.codec_context.sample_rate,
                                   'channels': s.codec_context.channels,
                                   'language': s.metadata.get('language')} for i, s in enumerate(audio)]}


def get_job(path):
    p = Path(path).expanduser().resolve(strict=True)
    j = load(p)
    if j.get('schema_version') != 1:
        raise ValueError('job.schema_version 必须为1')
    if not j.get('sources') or not j.get('characters'):
        raise ValueError('任务需要 sources 和 characters')
    resolve = lambda v: (p.parent / Path(v).expanduser()).resolve()
    j['_output'] = resolve(j.get('output_dir', 'output'))
    ids, names = set(), set()
    for s in j['sources']:
        sid = safe_name(s['id'])
        if sid in ids:
            raise ValueError(f'重复素材ID: {sid}')
        ids.add(sid)
        s['_path'] = resolve(s['path'])
        file_id(s['_path'])
        track = s.get('audio_stream', 0)
        if type(track) is not int or track < 0:
            raise ValueError('audio_stream 须为从0开始的音轨序号')
        s['audio_stream'] = track
    for c in j['characters']:
        name = safe_name(c['name'])
        if name.casefold() in names:
            raise ValueError(f'角色名重复: {name}')
        names.add(name.casefold())
    target = j.get('target_seconds', [120, 180])
    if len(target) != 2 or not 0 < target[0] <= target[1]:
        raise ValueError('target_seconds 应为 [最短秒数, 最长秒数]')
    j['target_seconds'] = target
    gap = j.get('gap_seconds', .18)
    if not isinstance(gap, (float, int)) or not math.isfinite(gap) or not 0 <= gap <= 2:
        raise ValueError('gap_seconds 应在0到2秒之间')
    return j


def source(j, sid):
    for s in j['sources']:
        if s['id'] == sid:
            return s
    raise ValueError(f'素材ID不存在: {sid}')


def source_key(s):
    return digest({'source': file_id(s['_path']), 'audio_stream': s['audio_stream'], 'revision': REVISION})


def prepared(j, s):
    out = j['_output'] / '.cache' / ('source-' + source_key(s))
    wav = out / 'mono16k.wav'
    marker = out / 'prepared.json'
    if not (wav.exists() and marker.exists()):
        out.mkdir(parents=True, exist_ok=True)
        info = probe(s['_path'])
        if s['audio_stream'] >= len(info['audio_streams']):
            raise ValueError(f"音轨序号超出范围: {s['id']}")
        temp = out / f'preparing-{os.getpid()}.wav'
        run_ff(['-y', '-i', s['_path'], '-map', f"0:a:{s['audio_stream']}", '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temp])
        import soundfile as sf
        info['analysis_duration_seconds'] = sf.info(temp).duration
        temp.replace(wav)
        dump(marker, info)
    return wav, load(marker)


def span(row, duration):
    start, end = float(row['start']), float(row['end'])
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + .03):
        raise ValueError(f'无效/越界时间段: {start}–{end}; 素材时长 {duration}')
    return start, min(end, duration)


def samples(wav, start, end):
    import soundfile as sf
    with sf.SoundFile(wav) as f:
        f.seek(round(start * f.samplerate))
        return f.read(round((end - start) * f.samplerate), dtype='float32')


def embedding(extractor, x):
    import numpy as np
    st = extractor.create_stream()
    st.accept_waveform(sample_rate=16000, waveform=np.ascontiguousarray(x))
    st.input_finished()
    if not extractor.is_ready(st):
        return None
    vec = np.asarray(extractor.compute(st), dtype=np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if np.isfinite(norm) and norm > 0 else None


def extractor_for(home):
    import sherpa_onnx as so
    return so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=str(home / 'models/embedding.onnx'), num_threads=2))


def reference_bank(j, home, extractor):
    import numpy as np
    bank = {}
    for c in j['characters']:
        vectors = []
        for r in c.get('references', []):
            if r.get('verified') is not True or not r.get('review_note', '').strip():
                raise ValueError(f"参考声音需记录身份核对依据: {c['name']}")
            s = source(j, r['source'])
            wav, info = prepared(j, s)
            a, b = span(r, info['analysis_duration_seconds'])
            key = digest({'source': source_key(s), 'start': a, 'end': b, 'model': file_id(home / 'models/embedding.onnx')})
            cache = j['_output'] / '.cache' / 'references' / (key + '.json')
            if cache.exists():
                v = np.array(load(cache), dtype=np.float32)
            else:
                v = embedding(extractor, samples(wav, a, b))
                if v is None:
                    raise ValueError(f"参考声音太短或不可提取声纹: {c['name']}")
                dump(cache, v.tolist())
            vectors.append(v)
        if vectors:
            avg = np.mean(vectors, axis=0)
            bank[c['name']] = avg / np.linalg.norm(avg)
    return bank


def analyze(args):
    import numpy as np
    import sherpa_onnx as so
    j = get_job(args.job); home = runtime(args)
    s = source(j, args.source); wav, info = prepared(j, s)
    start, end = span({'start': args.start, 'end': args.end if args.end is not None else info['analysis_duration_seconds']}, info['analysis_duration_seconds'])
    ex = extractor_for(home)
    bank = reference_bank(j, home, ex)
    settings = {'source': source_key(s), 'start': start, 'end': end, 'chunk_seconds': 300,
                'segmentation': file_id(home / 'models/segmentation.onnx'),
                'embedding': file_id(home / 'models/embedding.onnx'), 'revision': REVISION,
                'sherpa': importlib.metadata.version('sherpa-onnx')}
    cache = j['_output'] / '.cache' / ('analysis-' + digest(settings))
    cache.mkdir(parents=True, exist_ok=True)
    sd = None; rows = []
    offset = start; idx = 0
    while offset < end:
        stop = min(end, offset + 300)
        chunkfile = cache / f'{idx:04d}.json'
        if chunkfile.exists():
            chunkrows = load(chunkfile)
        else:
            if sd is None:
                sd = so.OfflineSpeakerDiarization(so.OfflineSpeakerDiarizationConfig(
                    segmentation=so.OfflineSpeakerSegmentationModelConfig(
                        pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(home / 'models/segmentation.onnx'), window_shift_ratio=.1), num_threads=2),
                    embedding=so.SpeakerEmbeddingExtractorConfig(model=str(home / 'models/embedding.onnx'), num_threads=2),
                    clustering=so.FastClusteringConfig(num_clusters=-1, threshold=.8), min_duration_on=.25, min_duration_off=.25))
            x = samples(wav, offset, stop); chunkrows = []
            for seg in sd.process(np.ascontiguousarray(x)).sort_by_start_time():
                a, b = max(0, float(seg.start)), min(len(x) / 16000, float(seg.end))
                if b <= a:
                    continue
                vec = embedding(ex, x[round(a * 16000):round(b * 16000)]) if b - a >= .65 else None
                chunkrows.append({'source': s['id'], 'start': offset + a, 'end': offset + b,
                                  'local_cluster': int(seg.speaker), 'chunk_start': offset,
                                  'chunk_boundary': a < .3 or stop - (offset + b) < .3,
                                  'embedding': vec.tolist() if vec is not None else None})
            dump(chunkfile, chunkrows)
        rows.extend(chunkrows)
        print(f"analyzed {s['id']} {offset:.1f}–{stop:.1f}s", flush=True)
        offset = stop; idx += 1
    for r in rows:
        v = r.pop('embedding')
        scores = {n: float(np.asarray(v) @ e) for n, e in bank.items()} if v is not None else {}
        order = sorted(scores, key=scores.get, reverse=True)
        best = order[0] if order else None
        margin = scores[best] - scores[order[1]] if len(order) > 1 else None
        overlap = any(o is not r and o['start'] < r['end'] - .05 and o['end'] > r['start'] + .05 for o in rows)
        r.update({'scores': scores, 'best_reference': best, 'margin': margin,
                  'possible_overlap': overlap,
                  'review_priority': 'candidate' if best and scores[best] >= args.threshold and (margin is None or margin >= args.margin) and not overlap else 'uncertain',
                  'verified': False})
    out = j['_output'] / '处理记录' / f"{s['id']}_{start:g}-{end:g}_候选.json"
    dump(out, {'settings': settings, 'threshold': args.threshold, 'margin_threshold': args.margin,
               'note': '候选分数不是概率；local_cluster只在当前300秒块内有效；未命中候选不等于没有该角色。', 'segments': rows})
    print(out)


def whisper_dir(home):
    root = home / 'models/whisper/models--Systran--faster-whisper-small/snapshots'
    candidates = sorted(p for p in root.glob('*') if (p / 'model.bin').exists() and (p / 'config.json').exists())
    if not candidates:
        raise ValueError('缺少本地 faster-whisper-small 模型，见 references/environment.md')
    return candidates[-1]


def transcribe(args):
    import soundfile as sf
    from faster_whisper import WhisperModel
    model_path = Path(args.model).expanduser().resolve() if args.model else whisper_dir(runtime(args))
    wav = Path(args.audio).expanduser().resolve(strict=True)
    info = sf.info(wav)
    if info.samplerate != 16000 or info.channels != 1:
        raise ValueError('转写输入须为16k单声道WAV；用prepare生成，或FFmpeg转换')
    start, end = span({'start': args.start, 'end': args.end if args.end is not None else info.duration}, info.duration)
    settings = {'audio': file_id(wav), 'model': file_id(model_path / 'model.bin'), 'language': args.language,
                'start': start, 'end': end, 'prompt': args.prompt, 'revision': REVISION}
    out = Path(args.output).expanduser().resolve()
    if out.exists() and load(out).get('settings') == settings:
        print(f'cached: {out}'); return
    model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=4, num_workers=1)
    segments, _ = model.transcribe(samples(wav, start, end), language=args.language, beam_size=3,
                                  word_timestamps=True, vad_filter=True, initial_prompt=args.prompt or None)
    rows = []
    for seg in segments:
        rows.append({'start': seg.start + start, 'end': seg.end + start, 'text': seg.text,
                     'words': [{'start': w.start + start, 'end': w.end + start, 'word': w.word,
                                'probability': w.probability} for w in seg.words or []]})
        print(f'{seg.start + start:.2f}–{seg.end + start:.2f} {seg.text}', flush=True)
    dump(out, {'settings': settings, 'segments': rows, 'note': '自动转写供定位核查，非校对字幕。时间相对传入音频。'})


def separate(home, raw, directory, model_name=MODEL):
    if Path(model_name).name != model_name or not model_name.endswith('.onnx'):
        raise ValueError('分离模型只能填写models/separator内的ONNX文件名')
    model = home / 'models/separator' / model_name
    if not model.exists():
        raise ValueError(f'缺少已验证的人声分离模型: {model}')
    bin_dir = home / 'bin'; bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / 'ffmpeg'
    if not link.exists():
        link.symlink_to(ffmpeg())
    os.environ['PATH'] = os.pathsep.join([str(bin_dir), '/usr/bin', '/bin', os.environ.get('PATH', '')])
    from audio_separator.separator import Separator
    sep = Separator(model_file_dir=str(model.parent), output_dir=str(directory), output_format='WAV',
                    output_single_stem='Vocals', use_soundfile=True, normalization_threshold=.95,
                    mdx_params={'segment_size': 256, 'overlap': .5, 'batch_size': 1, 'hop_length': 1024, 'enable_denoise': False})
    sep.load_model(model_filename=model_name)
    results = sep.separate(str(raw))
    if len(results) != 1:
        raise ValueError(f'人声分离输出异常: {results}')
    result = Path(results[0])
    if not result.is_absolute():
        result = directory / result
    return result


def reviewed_clips(j, character, infos):
    selected = []
    for row in character.get('clips', []):
        if row.get('verified') is not True or not row.get('review_note', '').strip():
            raise ValueError(f"{character['name']} 的选段尚缺核对依据；这不是要求用户审批，请先核对身份与边界")
        s = source(j, row['source'])
        a, b = span(row, infos[s['id']]['analysis_duration_seconds'])
        selected.append({**row, 'start': a, 'end': b})
    for i, a in enumerate(selected):
        for b in selected[i + 1:]:
            if a['source'] == b['source'] and a['start'] < b['end'] - .001 and a['end'] > b['start'] + .001:
                raise ValueError(f"{character['name']} 的选段重复或相互重叠")
    return selected


def quality_metrics(voice_path, original_path, upload_path, min_speech=30., max_upload_mb=10.):
    """Measure likely speech and gaps; these metrics never certify clean audio."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    voice_path = Path(voice_path).resolve(strict=True)
    upload_path = Path(upload_path).resolve(strict=True)
    voice, sr = sf.read(voice_path, dtype='float32', always_2d=True)
    mono = resample_poly(voice.mean(axis=1), 16000, sr)
    spans = get_speech_timestamps(mono, VadOptions(threshold=.3, min_speech_duration_ms=250,
                                                   min_silence_duration_ms=500, speech_pad_ms=0))
    gaps = ([spans[0]['start']] + [spans[i]['start'] - spans[i - 1]['end'] for i in range(1, len(spans))]
            + [len(mono) - spans[-1]['end']]) if spans else [len(mono)]
    speech_seconds = sum(s['end'] - s['start'] for s in spans) / 16000
    result = {'duration_seconds': len(mono) / 16000, 'estimated_speech_seconds': speech_seconds,
              'leading_nonspeech_seconds': gaps[0] / 16000, 'longest_nonspeech_seconds': max(gaps) / 16000,
              'upload_bytes': upload_path.stat().st_size,
              'checks': {'speech_target': speech_seconds >= min_speech,
                         'leading_gap': gaps[0] <= 16000,
                         'longest_gap': max(gaps) <= 32000,
                         'upload_size': upload_path.stat().st_size <= max_upload_mb * 1000000},
              'manual_listening_performed': False, 'music_free_verified': False,
              'note': 'VAD可能把音乐或哭腔误判为对白；分离指标和自动转写都不能代替听审。'}
    if original_path is not None:
        original, original_sr = sf.read(Path(original_path).resolve(strict=True), dtype='float32', always_2d=True)
        if original_sr != sr or original.shape != voice.shape:
            raise ValueError('质检需要原声对照和人声音轨具有相同时长、声道及采样率')
        raw = resample_poly(original.mean(axis=1), 16000, sr)[:len(mono)]
        nonspeech = np.ones(len(mono), dtype=bool)
        for s in spans:
            nonspeech[s['start']:s['end']] = False
        if nonspeech.sum() >= 1600 and len(mono) - nonspeech.sum() >= 1600:
            def ratio(x):
                return 20 * math.log10((float(np.sqrt(np.mean(x[nonspeech] ** 2))) + 1e-9) /
                                       (float(np.sqrt(np.mean(x[~nonspeech] ** 2))) + 1e-9))
            before, after = ratio(raw), ratio(mono)
            result['nonspeech_vs_speech_db_before'] = before
            result['nonspeech_vs_speech_db_after'] = after
            result['relative_background_improvement_db'] = before - after
    for path in (voice_path, upload_path):
        run_ff(['-i', path, '-f', 'null', '-'])
    result['checks']['decode'] = True
    result['status'] = 'candidate_needs_listening' if all(result['checks'].values()) else 'review_required'
    return result


def speaker_audit(j, character, clips, home, threshold=.45):
    """Flag clip-level mismatches; scores are only prompts for human review."""
    if not character.get('references'):
        return {'status': 'not_checked_no_reference', 'clips': []}
    import numpy as np
    ex = extractor_for(home)
    bank = reference_bank({**j, 'characters': [character]}, home, ex)
    ref = bank[character['name']]
    rows = []
    for r in clips:
        s = source(j, r['source'])
        wav, _ = prepared(j, s)
        vec = embedding(ex, samples(wav, r['start'], r['end']))
        score = float(vec @ ref) if vec is not None else None
        rows.append({'source': s['id'], 'start': r['start'], 'end': r['end'],
                     'reference_similarity': score, 'review_required': score is None or score < threshold})
    return {'status': 'review_required' if any(r['review_required'] for r in rows) else 'flag_check_passed',
            'threshold': threshold, 'note': '分数不是概率；情绪、哭腔和短句可降低相似度，未标记也不等于身份已证明。',
            'clips': rows}


def render(args):
    import numpy as np
    import soundfile as sf
    j = get_job(args.job); home = runtime(args)
    model_name = args.separator_model
    if not args.no_separate and (Path(model_name).name != model_name or not model_name.endswith('.onnx')):
        raise ValueError('分离模型只能填写models/separator内的ONNX文件名')
    chars = [c for c in j['characters'] if args.role is None or c['name'] == args.role]
    if not chars:
        raise ValueError(f'未找到角色: {args.role}')
    infos = {s['id']: prepared(j, s)[1] for s in j['sources']}
    report = {'title': j.get('title'), 'version': j.get('version'), 'characters': [], 'manual_listening_performed': False}
    for c in chars:
        name = c['name']; clips = reviewed_clips(j, c, infos)
        if not clips:
            report['characters'].append({'name': name, 'status': 'no_reviewed_clips'}); continue
        recipe = {'revision': REVISION, 'name': name, 'clips': clips, 'gap': j.get('gap_seconds', .18),
                  'sources': {sid: source_key(source(j, sid)) for sid in {r['source'] for r in clips}},
                  'separation': {'model': file_id(home / 'models/separator' / model_name), 'version': importlib.metadata.version('audio-separator')} if not args.no_separate else None}
        key = digest(recipe); cache = j['_output'] / '.cache' / ('render-' + key)
        dest = j['_output'] / name; dest.mkdir(parents=True, exist_ok=True); cache.mkdir(parents=True, exist_ok=True)
        reserved = [dest / f'{name}_{label}.{ext}' for label in ('去配乐', '原声对照') for ext in ('wav', 'mp3')]
        if any(p.resolve() == s['_path'] for p in reserved for s in j['sources']):
            raise ValueError('导出路径与原始素材重合，请更换output_dir')
        marker = dest / '处理记录.json'
        if marker.exists():
            prior = load(marker)
            if prior.get('recipe_key') == key and all(Path(f['path']).is_file() and sha256(f['path']) == f['sha256'] for f in prior.get('files', [])) and prior.get('files') and all((dest / f).is_file() for f in ['剪辑时间表.json', '剪辑时间表.csv']):
                lo, hi = j['target_seconds']; duration = prior['duration_seconds']
                prior['target_seconds'] = j['target_seconds']
                prior['status'] = 'under_target' if duration < lo else 'above_target' if duration > hi else 'within_target'
                label = '原声对照' if args.no_separate else '去配乐'
                prior['clone_qc'] = quality_metrics(dest / f'{name}_{label}.wav',
                                                    dest / f'{name}_原声对照.wav' if not args.no_separate else None,
                                                    dest / f'{name}_{label}.mp3', min_speech=lo)
                prior['speaker_audit'] = speaker_audit(j, c, clips, home)
                dump(marker, prior)
                print(f'cached: {name}', flush=True); report['characters'].append(prior); continue
        raw = cache / 'original.wav'; timeline = []; cursor = 0.; sr = 44100
        if raw.exists() and (cache / 'timeline.json').exists():
            timeline = load(cache / 'timeline.json')
        else:
            pieces = []
            for i, r in enumerate(clips):
                s = source(j, r['source']); part = cache / f'clip-{i:04d}.wav'
                run_ff(['-y', '-ss', r['start'], '-i', s['_path'], '-map', f"0:a:{s['audio_stream']}", '-t', r['end'] - r['start'], '-vn', '-ar', sr, '-ac', '2', '-c:a', 'pcm_s16le', part])
                x, _ = sf.read(part, dtype='float32', always_2d=True)
                if abs(len(x) / sr - (r['end'] - r['start'])) > .05:
                    raise ValueError(f'裁剪长度异常: {part}')
                n = min(round(.008 * sr), len(x) // 2)
                if n:
                    x[:n] *= np.linspace(0, 1, n)[:, None]; x[-n:] *= np.linspace(1, 0, n)[:, None]
                pieces.append(x)
                timeline.append({'clip': i + 1, 'source': s['id'], 'source_url': s.get('url', ''),
                                 'source_start': r['start'], 'source_end': r['end'],
                                 'output_start': round(cursor, 5), 'output_end': round(cursor + len(x) / sr, 5),
                                 'label': r.get('label', ''), 'review_note': r['review_note']})
                cursor += len(x) / sr
                if i < len(clips) - 1:
                    gap = np.zeros((round(recipe['gap'] * sr), 2), dtype='float32'); pieces.append(gap); cursor += len(gap) / sr
            sf.write(raw, np.concatenate(pieces), sr, subtype='PCM_16'); dump(cache / 'timeline.json', timeline)
        original, sr = sf.read(raw, dtype='float32', always_2d=True)
        variants = {'原声对照': original}
        if not args.no_separate:
            clean = cache / 'vocals.wav'
            if not clean.exists():
                produced = separate(home, raw, cache, model_name)
                produced.replace(clean)
            vocals, clean_sr = sf.read(clean, dtype='float32', always_2d=True)
            if clean_sr != sr or vocals.shape != original.shape:
                raise ValueError('人声分离改变了时长或声道，停止导出并检查')
            variants['去配乐'] = vocals
        if any(not np.isfinite(x).all() or float(np.sqrt(np.mean(x * x))) < 1e-6 for x in variants.values()):
            raise ValueError('输出静音或包含无效数据')
        peak = max(float(abs(x).max()) for x in variants.values()); gain = min(10 ** (-1.5 / 20) / peak, 4.)
        files = []
        for label, x in variants.items():
            path = dest / f'{name}_{label}.wav'; temp = dest / f'.{name}_{label}.tmp.wav'
            sf.write(temp, x * gain, sr, subtype='PCM_16'); temp.replace(path)
            mp3 = path.with_suffix('.mp3'); temp_mp3 = dest / f'.{name}_{label}.tmp.mp3'
            run_ff(['-y', '-i', path, '-c:a', 'libmp3lame', '-b:a', '192k', temp_mp3]); temp_mp3.replace(mp3)
            for f in (path, mp3):
                run_ff(['-i', f, '-f', 'null', '-'])
                files.append({'path': str(f), 'bytes': f.stat().st_size, 'sha256': sha256(f), 'decode': 'passed'})
        dump(dest / '剪辑时间表.json', timeline)
        with (dest / '剪辑时间表.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(timeline[0])); w.writeheader(); w.writerows(timeline)
        duration = len(original) / sr; lo, hi = j['target_seconds']
        status = 'under_target' if duration < lo else 'above_target' if duration > hi else 'within_target'
        result = {'name': name, 'recipe_key': key, 'status': status, 'duration_seconds': duration,
                  'selected_source_seconds': sum(r['end'] - r['start'] for r in clips), 'clip_count': len(clips),
                  'pair_gain_db': 20 * math.log10(gain), 'sample_peak': peak * gain,
                  'sample_rate': sr, 'channels': 2, 'target_seconds': j['target_seconds'],
                  'separation_model': model_name if not args.no_separate else None, 'files': files,
                  'manual_listening_performed': False,
                  'limitations': '自动解码、时长和峰值检查不等于听审；转写及身份复核见任务记录。'}
        if '去配乐' in variants:
            result['separation_delta_rms_ratio'] = float(np.sqrt(np.mean((variants['去配乐'] - original) ** 2)) / np.sqrt(np.mean(original ** 2)))
        label = '原声对照' if args.no_separate else '去配乐'
        result['clone_qc'] = quality_metrics(dest / f'{name}_{label}.wav',
                                             dest / f'{name}_原声对照.wav' if not args.no_separate else None,
                                             dest / f'{name}_{label}.mp3', min_speech=lo)
        result['speaker_audit'] = speaker_audit(j, c, clips, home)
        dump(marker, result); report['characters'].append(result)
        print(f'{name}: {duration:.2f}s, {status}, {dest}', flush=True)
    dump(j['_output'] / ('交付检查_' + safe_name(args.role) + '.json' if args.role else '交付检查.json'), report)


def qc(args):
    result = quality_metrics(args.voice, args.original, args.upload, args.min_speech, args.max_upload_mb)
    if args.output:
        dump(Path(args.output).expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cleanup(args):
    """Remove only recorded fetches and known cache directories after output verification."""
    job_path = Path(args.job).expanduser().resolve(strict=True)
    job = load(job_path)
    task_dir = job_path.parent
    output = (task_dir / job.get('output_dir', 'output')).resolve(strict=True)
    if not output.is_dir():
        raise ValueError('输出目录不存在，不能清理缓存')
    verified = []
    for character in job.get('characters', []):
        if not character.get('clips'):
            continue
        name = safe_name(character['name'])
        role_dir = output / name
        marker = role_dir / '处理记录.json'
        if not marker.is_file():
            raise ValueError(f'{name} 没有处理记录，不能清理缓存')
        record = load(marker)
        files = record.get('files', [])
        if not files or not (role_dir / '剪辑时间表.json').is_file():
            raise ValueError(f'{name} 成品或来源时间表不完整')
        timeline = load(role_dir / '剪辑时间表.json')
        if not timeline or any('source_url' not in row for row in timeline):
            raise ValueError(f'{name} 来源时间表缺少来源记录')
        for item in files:
            path = Path(item['path']).resolve(strict=True)
            if not path.is_relative_to(role_dir.resolve()) or sha256(path) != item['sha256']:
                raise ValueError(f'{name} 成品与处理记录不一致: {path}')
            run_ff(['-i', path, '-f', 'null', '-'])
            verified.append(str(path))
    if not verified:
        raise ValueError('未找到可核验的成品，不能清理缓存')

    removable = []
    published_updates = {}
    if args.published_root:
        published_root = Path(args.published_root).expanduser().resolve(strict=True)
        for character in job.get('characters', []):
            if not character.get('clips'):
                continue
            name = safe_name(character['name'])
            role_dir, published = output / name, published_root / name
            if not published.is_dir() or published.is_relative_to(role_dir) or role_dir.is_relative_to(published):
                raise ValueError(f'{name} 的交付副本目录无效')
            for item in role_dir.rglob('*'):
                if item.is_symlink():
                    raise ValueError(f'成品目录含符号链接，停止清理: {item}')
                if item.is_file() and (not (published / item.relative_to(role_dir)).is_file() or
                                       not filecmp.cmp(item, published / item.relative_to(role_dir), shallow=False)):
                    raise ValueError(f'{name} 的交付副本与任务目录不一致: {item}')
            record = load(published / '处理记录.json')
            for item in record['files']:
                item['path'] = str(published / Path(item['path']).name)
            published_updates[published / '处理记录.json'] = record
            removable.append(role_dir)
    cache = output / '.cache'
    if cache.exists():
        if cache.is_symlink() or not cache.is_dir():
            raise ValueError('缓存目录类型异常，停止清理')
        for child in cache.iterdir():
            if child.name in ('references', 'frames') or child.name.startswith(('source-', 'render-', 'analysis-')):
                if child.is_symlink():
                    raise ValueError(f'缓存中存在符号链接，停止清理: {child}')
                removable.append(child)
    for item in job.get('sources', []):
        src = (task_dir / Path(item['path']).expanduser()).resolve()
        record_path = src.parent / 'source.json'
        if not src.is_file() or not src.is_relative_to(task_dir) or not record_path.is_file() or not src.name.startswith('source.'):
            continue
        fetched = load(record_path)
        if (Path(fetched.get('path', '')).resolve() == src and
                fetched.get('requested_url') == item.get('url') and item.get('url')):
            removable.append(src)

    result = {'job': str(job_path), 'verified_outputs': verified,
              'would_remove': [{'path': str(p), 'bytes': sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) if p.is_dir() else p.stat().st_size}
                               for p in removable],
              'note': '仅清理技能生成的缓存及fetch片源；用户原素材和唯一成品保留。删除片源后需按来源URL重新下载。'}
    result['total_bytes'] = sum(x['bytes'] for x in result['would_remove'])
    if args.apply:
        for path in removable:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        if cache.is_dir() and not any(cache.iterdir()):
            cache.rmdir()
        for path, record in published_updates.items():
            dump(path, record)
        result['applied'] = True
        dump(output / '缓存清理记录.json', result)
    else:
        result['applied'] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))


def fetch(args):
    import yt_dlp
    from urllib.parse import urlparse
    if urlparse(args.url).scheme not in ('https', 'http'):
        raise ValueError('仅接受公开HTTP(S)视频页面')
    dest = Path(args.dest).expanduser().resolve(); dest.mkdir(parents=True, exist_ok=True)
    record = dest / 'source.json'
    if record.exists():
        prior = load(record)
        if prior['requested_url'] != args.url:
            raise ValueError('该目录已记录其他片源，请使用新目录')
        path = Path(prior['path'])
        if path.is_file() and path.stat().st_size == prior.get('bytes'):
            print(json.dumps(prior, ensure_ascii=False)); return
    if any(p.name != 'source.json' for p in dest.glob('source.*')):
        raise ValueError('目录有未完成或未记录的source文件，请检查后使用独立目录')
    opts = {'format': 'best[ext=mp4]/best', 'outtmpl': str(dest / 'source.%(ext)s'),
            'noplaylist': True, 'ffmpeg_location': ffmpeg(), 'retries': 3, 'fragment_retries': 3}
    with yt_dlp.YoutubeDL(opts) as dl:
        info = dl.extract_info(args.url, download=True)
        path = Path(dl.prepare_filename(info)).resolve()
    result = {'requested_url': args.url, 'webpage_url': info.get('webpage_url', args.url),
              'title': info.get('title'), 'duration_seconds': info.get('duration'),
              'format_id': info.get('format_id'), 'path': str(path), 'bytes': path.stat().st_size}
    probe(path); dump(record, result); print(json.dumps(result, ensure_ascii=False))


def doctor(args):
    home = runtime(args); packages = {}
    for name in ['sherpa-onnx', 'faster-whisper', 'audio-separator', 'imageio-ffmpeg', 'numpy', 'soundfile', 'librosa', 'audioread', 'yt-dlp', 'av', 'torch', 'onnxruntime']:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    models = {s: (home / 'models' / s).is_file() for s in ['embedding.onnx', 'segmentation.onnx', 'separator/' + MODEL]}
    try:
        models['whisper_small'] = str(whisper_dir(home))
    except ValueError:
        models['whisper_small'] = False
    print(json.dumps({'python': sys.executable, 'runtime': str(home), 'ffmpeg': ffmpeg(), 'packages': packages, 'models': models}, ensure_ascii=False, indent=2))
    if not all(packages.values()) or not all(models.values()):
        raise SystemExit(2)


def main():
    # platform.processor() and separator hardware detection call system utilities.
    os.environ['PATH'] = os.pathsep.join(['/usr/bin', '/bin', '/usr/sbin', '/sbin', os.environ.get('PATH', '')])
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', default=os.environ.get('FILM_VOICE_HOME', str(Path.home() / '.codex/tools/film-voice')))
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    q = sub.add_parser('probe'); q.add_argument('paths', nargs='+')
    q = sub.add_parser('fetch'); q.add_argument('--url', required=True); q.add_argument('--dest', required=True)
    q = sub.add_parser('prepare'); q.add_argument('--job', required=True)
    q = sub.add_parser('analyze'); q.add_argument('--job', required=True); q.add_argument('--source', required=True)
    q.add_argument('--start', type=float, default=0); q.add_argument('--end', type=float)
    q.add_argument('--threshold', type=float, default=.5); q.add_argument('--margin', type=float, default=.1)
    q = sub.add_parser('transcribe'); q.add_argument('--audio', required=True); q.add_argument('--output', required=True)
    q.add_argument('--start', type=float, default=0); q.add_argument('--end', type=float)
    q.add_argument('--model'); q.add_argument('--language', default='zh'); q.add_argument('--prompt', default='')
    q = sub.add_parser('render'); q.add_argument('--job', required=True); q.add_argument('--role'); q.add_argument('--no-separate', action='store_true')
    q.add_argument('--separator-model', default=MODEL)
    q = sub.add_parser('qc'); q.add_argument('--voice', required=True); q.add_argument('--original')
    q.add_argument('--upload', required=True); q.add_argument('--min-speech', type=float, default=30.)
    q.add_argument('--max-upload-mb', type=float, default=10.); q.add_argument('--output')
    q = sub.add_parser('cleanup'); q.add_argument('--job', required=True); q.add_argument('--published-root')
    q.add_argument('--apply', action='store_true')
    args = p.parse_args()
    if args.command == 'probe':
        print(json.dumps([probe(Path(x).expanduser().resolve(strict=True)) for x in args.paths], ensure_ascii=False, indent=2))
    elif args.command == 'prepare':
        j = get_job(args.job); result = []
        for s in j['sources']:
            wav, info = prepared(j, s); result.append({'source': s['id'], 'analysis_wav': str(wav), **info})
        dump(j['_output'] / '处理记录/素材检查.json', result); print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        globals()[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
