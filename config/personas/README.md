# 人物库（Persona Library）

> 作用：**随时改人物、随时换人，不用重启任何进程。**

## 三个位置

| 位置 | 角色 | 谁写 |
| --- | --- | --- |
| `config/persona.yaml` | **当前激活**的人物 | 你手改，或 `studio persona use <id>` 覆盖 |
| `config/personas/<id>.yaml` | **人物库**（备选人物，每人一个文件） | 你手写，或 `studio persona save-as <id>` |
| `data/backups/persona/` | 切换时的**自动备份**（回滚用） | 系统写 |

## 常用操作

```powershell
studio persona list                  # 看当前激活 + 库里有哪些人物
studio persona show                  # 看当前人物的完整定义与 sha256/version
studio persona use solo_commentary   # 一键换成"单人解说"
studio persona save-as my_v2         # 把当前改好的存进库（随时切回）
studio persona validate              # 校验激活人物 + 全部库条目
```

## 热重载行为（重要）

- 直接编辑 `config/persona.yaml` 保存后，**最迟在下一个任务/句子开始时生效**，无需重启。
- 5 个进程（API / draft / voice / render / publish）各自靠文件 mtime 独立发现变更，无 IPC。
- 编辑过程中写坏了 YAML ⇒ **不会中断正在跑的任务**：系统保留上一份可用快照，
  并在 `studio persona validate` / WebUI 上报错，改好即自动恢复。
- 每次内容变化，进程内 `version` 自增；变更事件会广播为 WS `system.persona_changed`。

## 新增一个人物

1. `Copy-Item config\personas\solo_commentary.yaml config\personas\我的新人物.yaml`
2. 改文件里的 `id`（**必须与文件名一致**）、`name`、`role_desc`、`tone`、`audience`、`catchphrases`、`forbidden`
3. `studio persona validate` 确认无误
4. `studio persona use 我的新人物` 切换

## 字段速查

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 人物标识，须与文件名一致（小写字母/数字/下划线/连字符） |
| `name` | ✅ | 展示名（可随时改，不影响历史数据） |
| `role_desc` | ✅ | 谁在说话、各自角色 |
| `tone` | ✅ | 语气 / 方言 / 节奏 |
| `audience` | ✅ | 受众画像 |
| `catchphrases` | ✅ | 口癖，**至少 2 条**（审稿要求命中 ≥2 个） |
| `forbidden` | ✅ | 禁区**原子词**（不要写整句，否则子串匹配不到） |
| `style_hint` | — | 直接拼进 Writer 的 system 提示词 |
| `target_chars_min/max` | — | 篇幅区间（默认 600–800） |
| `max_duration_ms` | — | 成片时长上限（默认 180000 = 3 分钟） |