"""提示词库：注册表 + 版本 + 渲染（T1.8 · §04.1.1 第 1 步 / P5 可复现）。

为什么要有注册表
----------------
``prompt_version`` 必须落库（``scripts.prompt_version``），否则"同输入同产物"无法复现。
版本 = 清单里的 ``version`` + 模板文件内容摘要 ⇒ **改一个字，版本就变**，
不需要人工记得去 bump 版本号（忘记 bump 正是漂移的常见来源）。

模板语法（**不引入 Jinja2**）
----------------------------
§1.6.2 的依赖矩阵没有 Jinja2，故这里实现一个**极小子集**：只做 ``{{变量}}`` 替换。

- 出现 ``{%`` / ``{#``（控制流 / 注释）⇒ **直接报错**，绝不静默当字面量输出；
  控制流交给 Python 组装 payload（能写单测、能类型检查，比模板里的循环可靠）。
- 变量缺失 ⇒ **报错**（不渲染成空串）：静默的空值会让提示词悄悄退化。

清单 ``prompts/manifest.yaml`` 的 ``sha256`` 覆盖该条目列出的**全部文件**：
``sha256_hex(*(f"{相对路径}\x1f{内容}" ...))``。

运行期覆盖层（T6.1 追加）
------------------------
``override_root`` 非空时，:meth:`PromptLibrary.read` **先看覆盖目录**
（``data/prompts/<相对路径>``），没有才读仓库里那份。两个刻意的取舍：

1. **覆盖不改仓库文件**：``prompts/`` 是入库的，``manifest.yaml`` 的 sha256
   逐字校验它；面板上改一个提示词就动仓库文件，等于让 git 里那份不再等于
   实际跑的那份。
2. **版本跟着覆盖走**：``digest()`` 走的是 :meth:`read` ⇒ 覆盖之后
   ``prompt_version`` 自动变（落 ``scripts.prompt_version``），不需要谁记得
   去 bump 一个号。而 :meth:`verify` 比对的是**仓库那一份**（漂移检查的对象
   是入库文件，覆盖不该被报成漂移）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from studio.core.errors import ErrorCode, LlmError
from studio.core.ids import sha256_hex
from studio.core.paths import StudioPaths

__all__ = [
    "MANIFEST_NAME",
    "PromptEntry",
    "PromptLibrary",
    "PromptManifest",
    "RenderedPrompt",
    "render_template",
    "template_variables",
]

MANIFEST_NAME: Final[str] = "manifest.yaml"

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_FORBIDDEN: Final[tuple[str, ...]] = ("{%", "{#")


def template_variables(text: str) -> frozenset[str]:
    """模板里引用到的变量名（``{{x}}`` ⇒ ``{"x"}``）。"""
    return frozenset(match.group(1) for match in _VAR.finditer(text))


class PromptEntry(BaseModel):
    """一个提示词条目：文件 + 版本 + 内容摘要。"""

    model_config = ConfigDict(extra="forbid")

    system: str | None = None
    user: str | None = None
    version: str = Field(default="1", min_length=1)
    sha256: str = Field(min_length=8)
    description: str = ""

    def files(self) -> tuple[str, ...]:
        return tuple(path for path in (self.system, self.user) if path)


class PromptManifest(BaseModel):
    """``prompts/manifest.yaml``（注册表本身也入 git ⇒ 换机可校验）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    prompts: dict[str, PromptEntry] = Field(default_factory=dict)


class RenderedPrompt(BaseModel):
    """渲染结果：system / user 文本 + 可落库的 ``prompt_version``。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    system: str
    user: str
    prompt_version: str


def render_template(text: str, variables: Mapping[str, str], *, name: str) -> str:
    """把 ``{{变量}}`` 替换为取值（缺失 / 非法语法 ⇒ 报错）。"""
    for token in _FORBIDDEN:
        if token in text:
            raise LlmError(
                f"提示词模板 {name} 含不支持的语法 {token}（本库只支持 {{{{变量}}}} 替换）",
                code=ErrorCode.LLM_PROMPT_MISSING,
                context={"prompt": name, "token": token},
                remediation="把控制流移到 Python 侧组装，模板里只留变量占位",
            )

    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.append(key)
            return ""
        return variables[key]

    rendered = _VAR.sub(_replace, text)
    if missing:
        raise LlmError(
            f"提示词模板 {name} 缺少变量：{sorted(set(missing))}",
            code=ErrorCode.LLM_PROMPT_MISSING,
            context={"prompt": name, "missing": sorted(set(missing))},
            remediation="调用方补齐变量（persona / payload 字段名要与模板一致）",
        )
    return rendered


class PromptLibrary:
    """提示词库（进程内单例语义：只读，可安全并发渲染）。"""

    def __init__(self, root: Path, manifest: PromptManifest, override_root: Path | None = None) -> None:
        self._root = root
        self._manifest = manifest
        self._override_root = override_root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def override_root(self) -> Path | None:
        """覆盖目录（``None`` ⇒ 这个库不认覆盖，只有仓库那一份）。"""
        return self._override_root

    @property
    def manifest(self) -> PromptManifest:
        return self._manifest

    @classmethod
    def load(cls, root: Path | None = None, *, override_root: Path | None = None) -> PromptLibrary:
        """载入 ``prompts/manifest.yaml``（缺文件 ⇒ ``LLM_PROMPT_MISSING``）。

        ``override_root`` 由入口给（``paths.prompts_override_dir``）—— 不在这里
        从 ``root`` 反推：测试里 ``root`` 常是 tmp，反推会去读**真的** ``data/prompts``，
        于是一个用例写的覆盖会悄悄影响另一个用例。
        """
        base = root if root is not None else StudioPaths.from_env().prompts_dir
        manifest_path = base / MANIFEST_NAME
        if not manifest_path.is_file():
            raise LlmError(
                f"提示词注册表不存在：{manifest_path}",
                code=ErrorCode.LLM_PROMPT_MISSING,
                context={"path": str(manifest_path)},
                remediation="确认 prompts/manifest.yaml 已入库（§02.2）",
            )
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        return cls(base, PromptManifest.model_validate(raw), override_root=override_root)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifest.prompts))

    def has(self, name: str) -> bool:
        return name in self._manifest.prompts

    def entry(self, name: str) -> PromptEntry:
        if name not in self._manifest.prompts:
            raise LlmError(
                f"提示词未注册：{name}",
                code=ErrorCode.LLM_PROMPT_MISSING,
                context={"prompt": name, "known": list(self.names())},
                remediation="在 prompts/manifest.yaml 追加条目（每个 Agent 落地时登记）",
            )
        return self._manifest.prompts[name]

    def read(self, relative: str) -> str:
        """读**生效**的那一份（覆盖目录优先）。"""
        override = self.override_path(relative)
        path = override if override is not None else self._root / relative
        if not path.is_file():
            raise LlmError(
                f"提示词文件不存在：{path}",
                code=ErrorCode.LLM_PROMPT_MISSING,
                context={"path": str(path)},
                remediation="补齐文件，或修正 manifest.yaml 里的相对路径",
            )
        return path.read_text(encoding="utf-8")

    def read_repo(self, relative: str) -> str:
        """读**仓库那一份**（忽略覆盖）—— ``verify`` 与「还原」的判据。"""
        path = self._root / relative
        if not path.is_file():
            raise LlmError(
                f"提示词文件不存在：{path}",
                code=ErrorCode.LLM_PROMPT_MISSING,
                context={"path": str(path)},
                remediation="补齐文件，或修正 manifest.yaml 里的相对路径",
            )
        return path.read_text(encoding="utf-8")

    def override_path(self, relative: str) -> Path | None:
        """这一份的覆盖文件（没有覆盖 ⇒ ``None``）。"""
        if self._override_root is None:
            return None
        path = self._override_root / relative
        return path if path.is_file() else None

    def is_overridden(self, relative: str) -> bool:
        return self.override_path(relative) is not None

    def write_override(self, relative: str, text: str) -> Path:
        """写一份覆盖（**调用方负责校验**：见 ``PromptService.save``）。"""
        if self._override_root is None:
            raise LlmError(
                "这个提示词库没有配覆盖目录",
                code=ErrorCode.CONFIG_INVALID,
                context={"prompt": relative},
                remediation="入口应传 paths.prompts_override_dir",
            )
        path = self._override_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def clear_override(self, relative: str) -> bool:
        """删掉一份覆盖（``True`` = 真的删了一份）⇒ 退回仓库那一份。"""
        path = self.override_path(relative)
        if path is None:
            return False
        path.unlink()
        return True

    def allowed_variables(self, name: str) -> frozenset[str]:
        """这个条目在渲染时**拿得到**的变量集合。

        取「自己的模板」∪「全部 ``shared.*`` 注入块」：Agent 的实际提示词就是
        ``compose(主模板 + 公共块)``，所以这些文件里出现过的变量都是运行期真的
        会被填上的。据此可以拦住"覆盖里写了一个没人传的变量" —— 那种错不会在
        保存时报，只会在下一次生成时炸，而那时人早忘了自己改过什么。

        读的是**仓库那一份**（``read_repo``）而不是生效那一份：调用方传哪些变量由
        Python 侧决定，模板只是它的镜像。若这里跟着覆盖走，那么"覆盖里新增一个
        ``{{新变量}}``"会把自己的名字算进允许集 ⇒ 校验必然通过 ⇒ 下一次生成才炸，
        正好是这条校验要拦的那件事。
        """
        entry = self.entry(name)
        relatives = list(entry.files())
        for other, other_entry in self.iter_entries():
            if other.startswith("shared."):
                relatives.extend(other_entry.files())
        found: set[str] = set()
        for relative in relatives:
            found |= template_variables(self.read_repo(relative))
        return frozenset(found)

    def digest(self, entry: PromptEntry) -> str:
        """条目内容摘要（路径 + 内容一起算 ⇒ 改名也会变）。

        走 :meth:`read`（覆盖优先）⇒ 覆盖之后 ``prompt_version`` 跟着变。
        """
        parts: list[str] = []
        for relative in entry.files():
            parts.extend([relative, self.read(relative)])
        return sha256_hex(*parts)

    def repo_digest(self, entry: PromptEntry) -> str:
        """仓库那一份的摘要（``verify`` 用）。"""
        parts: list[str] = []
        for relative in entry.files():
            parts.extend([relative, self.read_repo(relative)])
        return sha256_hex(*parts)

    def prompt_version(self, name: str) -> str:
        """``<version>+<sha12>`` —— 落 ``scripts.prompt_version`` 的可复现值。"""
        entry = self.entry(name)
        return f"{entry.version}+{self.digest(entry)[:12]}"

    def render(self, name: str, **variables: str) -> RenderedPrompt:
        """渲染 system / user 两段（缺哪段就返回空串，交由调用方决定是否合法）。"""
        entry = self.entry(name)
        system = render_template(self.read(entry.system), variables, name=name) if entry.system else ""
        user = render_template(self.read(entry.user), variables, name=name) if entry.user else ""
        return RenderedPrompt(
            name=name,
            system=system,
            user=user,
            prompt_version=f"{entry.version}+{self.digest(entry)[:12]}",
        )

    def combined_version(self, name: str, *extra: str) -> str:
        """把**若干条目**一起算出的 ``prompt_version``。

        为什么组合版本必须覆盖公共注入块
        --------------------------------
        Agent 的实际提示词 = ``shared.json_contract`` + ``shared.persona_block`` +
        自己的模板。若版本只算自己的模板，那么"改了 persona 注入块"这件事
        在 ``scripts.prompt_version`` 里**看不出来** ⇒ P5「同输入同产物」失效。
        """
        entry = self.entry(name)
        digest = sha256_hex(*(self.digest(self.entry(item)) for item in (name, *extra)))
        return f"{entry.version}+{digest[:12]}"

    def compose(
        self,
        name: str,
        *,
        system_blocks: Sequence[str] = (),
        user_blocks: Sequence[str] = (),
        **variables: str,
    ) -> RenderedPrompt:
        """渲染"主模板 + 公共注入块"，返回**一份**可直接交给网关的提示词。

        顺序固定：先公共块，后主模板（公共块是"通用纪律"，主模板是"这次干什么"）。
        """
        entry = self.entry(name)
        system_parts = [self.render(block, **variables).system for block in system_blocks]
        user_parts = [self.render(block, **variables).user for block in user_blocks]
        if entry.system:
            system_parts.append(render_template(self.read(entry.system), variables, name=name))
        if entry.user:
            user_parts.append(render_template(self.read(entry.user), variables, name=name))
        return RenderedPrompt(
            name=name,
            system="\n\n".join(part for part in system_parts if part),
            user="\n\n".join(part for part in user_parts if part),
            prompt_version=self.combined_version(name, *system_blocks, *user_blocks),
        )

    def verify(self) -> list[str]:
        """漂移清单（空 = 与 manifest 一致）。供 ``studio prompts verify`` 与 CI 使用。"""
        problems: list[str] = []
        for name in self.names():
            entry = self.entry(name)
            if not entry.files():
                problems.append(f"{name}: 未登记任何文件")
                continue
            for relative in entry.files():
                if not (self._root / relative).is_file():
                    problems.append(f"{name}: 文件缺失 {relative}")
            if problems and problems[-1].startswith(name):
                continue
            # 比对的是**仓库那一份**：漂移检查的对象是入库文件，运行期覆盖
            # 不该被报成漂移（否则面板上改一个字，`prompts verify` 就红了）。
            actual = self.repo_digest(entry)
            if actual != entry.sha256:
                problems.append(f"{name}: 内容漂移（manifest={entry.sha256[:12]} 实际={actual[:12]}）")
        return problems

    def iter_entries(self) -> Iterable[tuple[str, PromptEntry]]:
        return ((name, self.entry(name)) for name in self.names())
