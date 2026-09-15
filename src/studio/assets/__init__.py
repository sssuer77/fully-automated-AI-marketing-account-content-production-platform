"""素材库（T4.8 · §3.3.14 / §04.2.8 / §4.3.1）。

三类素材、三套形态
------------------
- ``broll``  跑酷视频：``data/assets/mc_parkour/parkour_*.mp4``（**平铺文件**）
- ``bgm``    背景乐：  ``data/assets/bgm/bgm_*.mp3``（平铺文件）
- ``voice``  零样本参考音：``data/voice_src/<voice_id>/{ref_01.wav, ref.txt, profile.json}``
  （**一音色一目录**）

为什么形态不一样
----------------
跑酷与 BGM 的形态是**冻结契约**：模板配置里写的是通配
（``data/assets/mc_parkour/parkour_*.mp4``，§03.6.1），T3 按通配抽素材。改成
一素材一目录等于改一份冻结契约，而它换来的能力（"放文件 + 重扫"）平铺同样有。

音色反过来：参考音是**一组**文件（2–3 段 + 逐字文本 + 来源登记），平铺表达不了
"这几段属于同一个音色"，所以它是一音色一目录。

id 就是文件名的 stem
--------------------
``parkour_017.mp4`` ⇒ id ``parkour_017``；``bgm_003.mp3`` ⇒ id ``bgm_003``；
``data/voice_src/bigbear/`` ⇒ id ``bigbear``。不做"去掉前缀"的变换：那会造出第二个
命名空间（"库里的 id"与"磁盘上的名字"），而面板上显示的、日志里写的、``broll_usage``
里存的三处必须一眼对得上。

本包的分工
----------
- :mod:`studio.assets.layout`   —— 目录约定、id 规则、扫盘发现（**不碰数据库**）；
- :mod:`studio.assets.validate` —— 每类素材的合格判据（**不碰数据库**）；
- 入库（写 ``broll_clips`` / ``bgm_tracks``）与 REST 面见后续模块。
"""
