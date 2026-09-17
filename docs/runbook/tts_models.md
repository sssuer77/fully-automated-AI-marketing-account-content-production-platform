# CosyVoice 权重与推理环境（T2.1 · E5）

> 2026-09-17 真机跑通：**权重加载 10.4s / 一句合成 8.4s（7.72s 音频，RTF 1.09）**。
> 这份手册记的是「怎么装回来的」—— 下一次换机器 / 重装系统时照敲即可。

## 一、东西在哪

| 东西 | 路径 | 体积 |
| --- | --- | --- |
| 模型权重 | `D:\ai_models\modelscope_cache\models\iic--CosyVoice2-0.5B\snapshots\master` | **5.23 GB / 21 文件** |
| 推理源码 | `D:\ai_models\CosyVoice` | revision **`074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`**（2026-05-26） |
| 子模块 Matcha-TTS | `D:\ai_models\CosyVoice\third_party\Matcha-TTS` | revision **`dd9105b34bf2be2230f4aa1e4769fb586a3c824e`** |
| Python 环境 | `tts\.venv`（Python 3.11.15 · torch **2.4.0+cu121** · CUDA True） | — |

**两个都不在仓库里**（`.gitignore` 覆盖 `models/`；源码放在 D 盘是为了不把 5 GB 权重
和一份第三方仓库塞进 git）。所以「换一台机器」= 重新跑下面两条命令。

## 二、怎么装回来

```powershell
# ① 权重（ModelScope；约 1–2 分钟，30–40 MB/s）
uv run --project tts python -c "from modelscope import snapshot_download; print(snapshot_download('iic/CosyVoice2-0.5B', revision='master'))"

# ② 源码（含子模块）
git clone --depth 1 --recurse-submodules --shallow-submodules https://github.com/FunAudioLLM/CosyVoice.git D:\ai_models\CosyVoice

# ③ 推理依赖（**不要**照抄上游 requirements.txt，见下）
uv pip install --python tts\.venv\Scripts\python.exe -r tts\requirements-cosyvoice.txt
uv pip install --python tts\.venv\Scripts\python.exe "setuptools<81"   # whisper 的 setup.py 要 pkg_resources
uv pip install --python tts\.venv\Scripts\python.exe --no-build-isolation openai-whisper==20231117
```

### 为什么不直接 `-r CosyVoice/requirements.txt`

那份文件写着 `torch==2.3.1` / `torchaudio==2.3.1`，而本项目锁的是 **torch 2.4.0+cu121**
（`tts/pyproject.toml`：只有 `D:\Torch` 里有 cp311 的 cu121 wheel）。照单全收会把 torch
降级 —— 那是把一个**已验证能跑**的环境换成另一个要重新验证的环境。
`tts/requirements-cosyvoice.txt` 只列推理真正用到的那些，并且**故意不写 torch**。

三个非显而易见的坑（都踩过）：

- `openai-whisper==20231117` 的 `setup.py` 要 `pkg_resources`，而 setuptools ≥ 81 已把它删掉
  ⇒ 必须 `setuptools<81` + `--no-build-isolation`。不装它整个包 import 不了
  （`cosyvoice/cli/frontend.py` 在**模块级**就 import whisper）。
- `pyarrow` / `pyworld` 也要装：`cosyvoice/dataset/processor.py` 被间接 import 到。
- 上游 `requirements.txt` 里的 `deepspeed` / `tensorrt` / `vllm` 是 **Linux 专属**，
  Windows 上不装（代码里都是条件 import）。

## 三、怎么验

```powershell
$env:PYTHONPATH = "D:\ai_models\CosyVoice;D:\ai_models\CosyVoice\third_party\Matcha-TTS"
tts\.venv\Scripts\python.exe -c "from cosyvoice.cli.cosyvoice import CosyVoice2; print('ok')"
```

**`PYTHONPATH` 这两段都不能少**：第一段是 CosyVoice 本体，第二段是 Matcha-TTS
（`cosyvoice.flow.flow_matching` 依赖 `matcha` 包；少了它的报错是
`pydoc.ErrorDuringImport: ... No module named 'matcha'`，看上去完全不像是路径问题）。

一次完整的零样本克隆（用**项目自己的参考音**，不是上游示例音）：

```python
import torch, soundfile as sf
from cosyvoice.cli.cosyvoice import CosyVoice2

MODEL = r"D:\ai_models\modelscope_cache\models\iic--CosyVoice2-0.5B\snapshots\master"
model = CosyVoice2(MODEL, load_jit=False, load_trt=False, load_vllm=False, fp16=False)
chunks = list(model.inference_zero_shot(
    "这是全自动制片台的第一句测试配音。",
    "随便说点什么都可以的。",
    "data/voice_src/bigbear/ref_01.wav",     # ← **路径**，不是张量
    stream=False,
))
audio = torch.cat([c["tts_speech"] for c in chunks], dim=1).squeeze(0).numpy()
sf.write("out.wav", audio, model.sample_rate)
```

真机读数（2026-09-17 · RTX 2080 SUPER 8 GB）：

| 项 | 读数 |
| --- | --- |
| `import` + 模型加载 | **10.4 s** |
| 合成一句 13 字中文 | **8.4 s** → 7.72 s 音频（**RTF 1.09**） |
| 显存占用 | **2.38 GB** |
| 采样率 | 24000 |
| 输出电平 | RMS 0.0071 / peak 0.0757（偏低是正常的 —— 交给 T3.6 的 `loudnorm` 抬） |

## 四、两个已知状态（不是故障）

1. **onnxruntime 跑在 CPU 上**。日志里那句
   `Specified provider 'CUDAExecutionProvider' is not in available provider names`
   是如实的：装的是 `onnxruntime`（CPU 版），不是 `onnxruntime-gpu`。
   上游的 `requirements.txt` 在 Windows 上也是这么写的（`onnxruntime==1.18.0; sys_platform == 'win32'`）。
   影响的是 campplus / speech_tokenizer 那几段（RTF 里的一小部分），**不影响能不能出片**。
   要提速再单独换 `onnxruntime-gpu`，换完必须重跑一次上面的验收。
2. **没有 `ttsfrd` 文本前端**，日志里是 `no frontend is avaliable`。CosyVoice 会退回内置
   的 `text_normalize`。这正是本项目要的：T2.5 的归一化 + glossary 自己管，
   **不引入 `pynini` / `WeTextProcessing`**（Windows 装不上，见 T2.5 的施工裁定）。

## 五、踩过的坑（写下来免得重踩）

- **参考音要传路径**。这个 revision 的 `inference_zero_shot` 把第三个参数直接交给
  `load_wav(prompt_wav, 24000)`，而 `load_wav` 只认路径。传一个**已经加载好的张量**进去，
  报的是 `TypeError: Invalid file: tensor([[...]])` —— 看起来像数据坏了，其实是不该先加载。
- **上游示例音不在快照里**。`snapshots/master/asset/` 只有一张 `dingding.png`；
  `example.py` 里用的 `./asset/zero_shot_prompt.wav` 是**仓库**里的文件，不是权重包里的。
  所以验收直接用 `data/voice_src/bigbear/ref_01.wav` —— 那也是真实用法。
- **torchaudio 的 `save` 在这个环境里会炸**（`Invalid file: tensor(...)`）。
  写文件用 `soundfile.write(path, tensor.squeeze(0).numpy(), sr)`。
