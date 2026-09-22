# 备份舱（backup-bay）— 人读手册

> **一句话**：git 之外的**本地时刻态灾难冗余**。一句话触发，把「这一刻不可再生的
> 全部状态」压成一个自足单体（`.tar.zst` + `.manifest.json`），单拎出来可离线复现。
> **零 git 耦合**——不调用、不读取、不产出任何 git 依赖，恢复也不靠 git。

---

## 一、什么时候用

| 场景 | 一句话 |
|------|--------|
| 动手大改之前 | 「阶段备份」 |
| 一段工作收口 | 「代码备份」 |
| 要出差 / 换机器 | 「出差备份」 |
| 吃过手滑的亏之后 | 「给这项目上备份」 |
| 想知道舱的新鲜度 | 「舱状态」 |
| 要恢复 | 「恢复 <舱名>」 |

**不自动打舱**：人不提，AI 就忽略。

---

## 二、30 秒上手

```bash
# 打一个舱（阶段/代码/出差 三选一，可带 tag 与说明）
bash scripts/project-bay-backup.sh 阶段
bash scripts/project-bay-backup.sh 代码 v1.24.8 "框架第四轮收口"

# 体检：新鲜度 / 可解压 / hash 一致性
bash scripts/project-bay-check.sh

# 恢复演练：**不落盘**，先看会发生什么（强烈建议先跑这个）
bash scripts/project-bay-restore.sh --dry-run <舱文件名>

# 真恢复
bash scripts/project-bay-restore.sh <舱文件名>
```

舱落在 `backups/`（已被 `.gitignore` 排除）。

---

## 三、0 基础恢复步骤（没有 AI 也能做）

1. **找到舱**：`ls backups/`，形如
   `ai-collab-methodology-code-20260922T141530Z.tar.zst` + 同名 `.manifest.json`。
2. **看里面有什么**（不落盘验证）：
   ```bash
   zstd -t backups/<舱名>.tar.zst          # 校验完整性
   tar --zstd -tf backups/<舱名>.tar.zst | head -50
   ```
3. **恢复到一个空目录**（不要直接覆盖在用目录）：
   ```bash
   mkdir -p /tmp/bay-restore && tar --zstd -xf backups/<舱名>.tar.zst -C /tmp/bay-restore
   ```
4. **看恢复说明**：舱内有 `RESTORE.txt`（依赖重建命令、原路径、生成时间）。
5. **核对**：`.manifest.json` 里的 `files` 与 hash 列表，和 `RESTORE.txt` 一起构成
   「这一刻是什么样」的证据。

> `project-bay-restore.sh` 只是把上面这几步自动化（含 hash 校验与 dry-run），
> **它不可用时按上面手做即可** —— 这是本舱"自足"的含义。

---

## 四、这个项目为什么需要它

本仓库全部内容都在 git 里，看起来"有 git 就够了"。但 git 防不住三类事：

1. **本地未提交 / 未推送的那一瞬** —— 工作区改到一半、或提交了还没推上去时，
   磁盘坏了就没了（本机到 GitHub 的通道本身不稳定）。
2. **误操作** —— `git reset --hard`、误删分支、rebase 出错；git 的兜底靠 reflog，
   而 reflog 有保留期、也可能被清。
3. **刻意不进 git 的东西** —— 本项目是 `.publish/`（发布前检查工具 + 一份私有标识
   清单）：git 完全兜不住。**处置是刻意不带上船**——舱可能被拷来拷去，不带私货。

---

## 五、边界（别把它当万灵药）

| 它**能** | 它**不能** |
|---|---|
| 复现「这一刻的磁盘状态」 | 替代异地容灾（舱在本机磁盘上，盘坏了舱也没了） |
| 兜住未提交/未推送的工作 | 增量备份（每次是全量，靠 `retain_code_bays` 轮换控制占用） |
| 单拎可离线复现 | 带 secrets —— 本项目的立场是**不带**（见 `backup-bay.conf`） |

**契约**：本项目的备份舱遵循框架标准 `backup-bay@0.1`（spec / schema / conformance 三层）。
