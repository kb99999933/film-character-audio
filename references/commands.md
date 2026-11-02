# 命令与任务格式

所有命令由执行者调用。当前机器的Python为 `/Users/lianyou/.codex/tools/film-voice/venv/bin/python`，脚本为 `/Users/lianyou/.codex/skills/film-character-audio/scripts/workflow.py`。命令示例中的短写可用以下变量：

```sh
VOICE_PY="$HOME/.codex/tools/film-voice/venv/bin/python"
VOICE_FLOW="$HOME/.codex/skills/film-character-audio/scripts/workflow.py"
"$VOICE_PY" "$VOICE_FLOW" doctor
```

`--runtime /绝对路径/运行环境` 是放在子命令之前的可选参数，或设置 `FILM_VOICE_HOME`。其中包含 `models/` 和 `bin/`；不打包大模型到Skill目录。

## 任务JSON

路径可以是绝对路径，也可相对任务JSON所在目录。下面是一个格式示例，时间和核对依据必须替换为本次素材实查结果。脚本不会自动把待确认片段改为已确认。

```json
{
  "schema_version": 1,
  "title": "红楼梦",
  "version": "1987版，播出版普通话原声",
  "output_dir": "output",
  "target_seconds": [30, 180],
  "gap_seconds": 0.18,
  "sources": [
    {"id": "ep04", "path": "sources/ep04/source.mp4", "audio_stream": 0, "url": "片源页面URL"}
  ],
  "characters": [
    {
      "name": "林黛玉",
      "references": [
        {"source": "ep04", "start": 434.58, "end": 436.50, "verified": true,
         "review_note": "央视该集此处‘我来得不巧了’，结合台词和画面确认；此时间不可套用其他剪辑版本"}
      ],
      "clips": [
        {"source": "ep04", "start": 433.70, "end": 436.80, "label": "来得不巧",
         "verified": true, "review_note": "与已确认参考相符，核对原片句首句尾，无他人插话"}
      ]
    }
  ]
}
```

- 新任务初始 `references` 和 `clips` 可为空，先准备和查看片源再填写。没有参考时analyze只分段，不赋予真实角色身份。
- 素材ID唯一；角色名用于输出目录，不能有路径分隔符等文件名非法字符。
- `audio_stream` 是从0开始的音轨序号，不是容器stream的全局index。多音轨先probe再选择。
- 时间单位秒，始终相对所登记源文件的起点。下载源片、用户混剪、截取过的小文件不能混用时间轴。
- `clips` 数组顺序就是合辑顺序；原句不能重复，不允许同角色在同一源上的选段重叠。不同角色之间的冲突由执行者复核。
- `target_seconds` 是任务时长区间；用于克隆时把首项设为用户接受的实际说话目标（例如30秒）。脚本不自动截断句子或循环凑时长，`render` 的总时长状态不能代替 `clone_qc.estimated_speech_seconds`。
- 身份已确定的纯角色短片可以直接选 `[0, 实际时长]`，不要求先运行声纹分析。

## 使用顺序

同一任务按prepare→analyze/transcribe→核对clips→render顺序执行，等待上一步退出后再启动依赖它的命令；不要同时对同一输出目录运行两个render。独立任务使用独立输出目录。

```sh
# 选定可公开播放的视频页面后下载；同目录同来源可复用
"$VOICE_PY" "$VOICE_FLOW" fetch --url 'https://tv.cctv.com/2009/12/27/VIDE1355623946576347.shtml' --dest '/绝对路径/任务/sources/ep04'

# 检查用户文件/下载片源；读取音轨和时长
"$VOICE_PY" "$VOICE_FLOW" probe '/绝对路径/素材.mp4'

# 按任务准备16k单声道分析音频，路径写入output/处理记录/素材检查.json
"$VOICE_PY" "$VOICE_FLOW" prepare --job '/绝对路径/任务/job.json'

# 可分场景分析，start/end相对完整源文件；不指定end则分析到结尾
"$VOICE_PY" "$VOICE_FLOW" analyze --job '/绝对路径/任务/job.json' --source ep04 --start 420 --end 700

# 输入prepare输出的mono16k.wav。先转写相关场景，不必每次转写整部剧
"$VOICE_PY" "$VOICE_FLOW" transcribe --audio '/绝对路径/mono16k.wav' --start 420 --end 700 --prompt '红楼梦。林黛玉，贾宝玉，薛宝钗。' --output '/绝对路径/任务/场景转写.json'

# 执行者完成clips核对之后导出。默认处理所有角色，可指定一个角色
"$VOICE_PY" "$VOICE_FLOW" render --job '/绝对路径/任务/job.json'
"$VOICE_PY" "$VOICE_FLOW" render --job '/绝对路径/任务/job.json' --role '林黛玉'

# 只在本机已有模型时比较短样；不同模型使用不同的output_dir，避免相互覆盖
"$VOICE_PY" "$VOICE_FLOW" render --job '/绝对路径/短样_另一输出目录/job.json' --separator-model 'Kim_Vocal_2.onnx'
```

导出后核对转写：用 `imageio_ffmpeg.get_ffmpeg_exe()` 返回的FFmpeg，将去配乐WAV通过 `-ar 16000 -ac 1` 转为检查副本，再运行 `transcribe`。转写输出时间相对该副本，不是原剧；用剪辑时间表回溯。

获取核对截图：使用源视频的明确时间，通过FFmpeg `-ss 秒数 -i 源视频 -frames:v 1 图片路径` 保存到任务输出的 `.cache/frames/` 后查看。单帧只能辅助确认场景，不能独立证明谁正在说话；任务结束后此目录可清理。

## 质检与清理

`render` 自动把 `clone_qc` 和 `speaker_audit` 写入角色的 `处理记录.json`。前者只估计说话时长、非对白空档、上传大小和相对背景声变化；后者在有已核对参考声音时标出低相似度片段，须结合画面和台词复核。两者都不能自动判定“可直接克隆”。对外部编辑过的成品单独运行：

```sh
"$VOICE_PY" "$VOICE_FLOW" qc --voice '/绝对路径/角色_去配乐.wav' --original '/绝对路径/角色_原声对照.wav' --upload '/绝对路径/角色_去配乐.mp3' --min-speech 30 --max-upload-mb 10 --output '/绝对路径/角色_质检.json'
```

任务结论确定且最终文件已放在交付目录后，先预览清理范围，再执行。`cleanup` 会重新校验每个已选角色的成品哈希、完整解码与来源时间表；只识别任务输出下的已知缓存目录，以及任务目录内由 `fetch` 产生、`source.json` 与任务URL一致的片源。用户原始素材不在删除范围。若把角色成品复制到另一目录，可用 `--published-root` 指向该目录；每个副本逐文件相同才会删除任务目录里的重复成品，并把交付副本的文件路径更新为当前位置。

```sh
"$VOICE_PY" "$VOICE_FLOW" cleanup --job '/绝对路径/任务/job.json'
"$VOICE_PY" "$VOICE_FLOW" cleanup --job '/绝对路径/任务/job.json' --apply
# 已复制角色目录到另一位置时：
"$VOICE_PY" "$VOICE_FLOW" cleanup --job '/绝对路径/任务/job.json' --published-root '/绝对路径/交付根目录' --apply
```

清理后保留 `source.json`、来源时间表和URL；需要复剪时用原URL重新执行 `fetch`。仍要继续补采的角色应延后清理。预览清理不改变任何文件。

## 缓存、重跑和输出

- 准备和分析缓存保存在 `output/.cache`。源文件路径/大小/修改时间、音轨、模型、区间等变化会产生新缓存；分析按300秒块落盘，中断后复用已完成块。
- 参考声纹按源片段和模型缓存；换角色参考时可复用原始分段结果，重新计算匹配分数。
- 导出配方包含源文件标识、角色、选段、间隔和分离模型。相同配方且成品哈希不变时跳过重做；原始拼接音频永远来自片源，不从已增益的成品回读。
- 源文件若被外部程序改动却刻意保持大小和修改时间，缓存不能感知；应使用新源文件或新输出目录。
- 渲染失败会留下缓存供恢复，但不会把该角色写成成功交付。改变为不去配乐模式时，建议用新输出目录，避免旧的去配乐文件与当前结果混放。
- `处理记录.json` 列出当前导出配方、时长、目标范围、峰值、格式、文件哈希和完整解码结果。`under_target`不是渲染失败，但交付时必须说明不足。
- `clone_qc` 的说话时长来自VAD，只是估计；空档、文件大小检查通过后仍标候选。`speaker_audit` 的分数也只是提示，短句与哭腔可能误报，不能凭高分宣称身份无误。
- `separation_delta_rms_ratio` 是波形变化量，不是“去音乐准确率”。不能以该值证明音质提升。
- 参考核对与最终剪辑核对由执行者负责；`manual_listening_performed: false` 是脚本的真实能力边界。
- 包含多个角色时逐一检查角色目录。`--role`生成该角色的独立交付检查，不能用旧的全任务报告代替本次结果。
