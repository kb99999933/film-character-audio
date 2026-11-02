# film-character-audio

影视角色原声音频提取 Skill。从用户提供的影视素材或公开视频中定位指定角色对白，剪辑、去配乐，并按角色导出音频及来源时间表。

## 能做什么

- 检查音轨、下载可公开读取的视频，保留来源记录。
- 用说话分段、参考声纹和转写辅助定位对白，再结合原片核对角色与剪辑边界。
- 每个角色单独导出原声对照及去配乐版 MP3 / WAV。
- 检查音频完整性、有效对白时长和非对白空档；成品校验通过后可清理任务缓存。

## 使用

将本仓库作为 `film-character-audio` Skill 安装到支持 `SKILL.md` 的 AI 工具中，提供作品版本、角色名、目标时长及本地素材路径或视频页面。

例如：

> 使用 film-character-audio，从我提供的 1987 版《红楼梦》视频中提取林黛玉约 30 秒的单人对白，保留原声语速，导出去配乐版、原声对照和来源时间表。

仓库包含 Skill 指令、Python 工作流和测试。运行依赖与模型需另外准备；已验证环境为 Apple Silicon Mac，默认运行环境目录为 `~/.codex/tools/film-voice`。

- [完整 Skill 指令](SKILL.md)
- [运行环境与模型要求](references/environment.md)
- [命令和任务 JSON 格式](references/commands.md)
- [实测案例及能力边界](references/verified-cases.md)

已有运行环境时，在仓库目录执行：

```sh
"$HOME/.codex/tools/film-voice/venv/bin/python" scripts/workflow.py doctor
"$HOME/.codex/tools/film-voice/venv/bin/python" -m unittest discover -s tests -v
```

自动分段和声纹分数只能辅助选段。角色身份、重叠对白和分离后的听感仍需复核；脚本不会把机器检查通过等同于已完成听审。真实素材测试默认跳过，须具备对应本地素材和模型后显式开启。
