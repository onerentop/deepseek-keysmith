# deepseek-keysmith

**DeepSeek Harness (dsh) 的受管理 true system-role 入口安装器 + 一等公民插件。**

`deepseek-keysmith` 把一个受管理的 `system-role.md` 接进 DeepSeek Harness 的 **system message 路径**，而不是普通的工作区指令（AGENTS.md）通道。它不改 harness 安装、不动任何 pnpm 依赖，只写 Harness home（默认 `~/.dsh`）。

## 这是什么

DeepSeek Harness 用 Cordis 插件树构建 prompt：所有 prompt 段（harness 身份、persona、工具说明）都汇入 `ctx.systemPrompt` 注册表，`renderPrompt(assembly)` 把它们拼成发给模型的 `system` 字段。**每个 profile 启动时都会读取 home 层补丁**：

```text
~/.dsh/cordis.patch.yml
```

安装器往这个补丁层插入一行，挂载一个迷你 Cordis 插件（`~/.dsh/keysmith/plugin.cjs`）。插件在 prompt 注册表注册 `managed:keysmith` 段（order 5，persona 之后、工具说明之前），内容实时读取 `~/.dsh/keysmith/system-role.md`。于是你的 system-role 进入的是 runtime 的 system message 管道，而不是用户角色的 AGENTS.md 通道。

## 默认写入位置

```text
~/.dsh/cordis.patch.yml                        # home 层补丁（dsh 本来就会读）
~/.dsh/keysmith/system-role.md                 # 受管理的 system prompt 主体
~/.dsh/keysmith/plugin.cjs                     # 插件 bundle（esbuild 打包的单文件）
~/.dsh/keysmith/config.json                    # 安装清单（路径、哈希、状态）
```

Harness 原包与 profile 目录保持完整：安装器不读取、不保存、不打印 API key / token / 会话数据。

## 原理

1. dsh 的每个 profile 启动时按序叠加补丁层：bundle 层 → profile 层 → **home 层**（`~/.dsh/cordis.patch.yml`）→ `--patch` 覆盖。
2. 安装器在 home 层插入 `- insert:` 行：`id: keysmith`，`name` 为插件 bundle 的 `file://` 绝对 URL（Node ESM 直接加载，无需 pnpm 注册），`config.systemFile` 用 `!!js` 表达式注入 `dshHomePath('keysmith/system-role.md')`（loader 上下文提供 `dshHomePath`）。
3. 插件 `apply(ctx, config)` 调用 `ctx.systemPrompt.section({ name: 'managed:keysmith', order: 5, text: () => ... })`。`text` 是惰性函数，每次 prompt 组装时读取文件，**改 `system-role.md` 下次对话即生效，不用重装**。
4. 插件本体是仓库内 `plugin/` 下的真 TypeScript 插件（`@deepseek-ai/dsh-keysmith`，真 Schemastery schema），用 esbuild 打包成单文件自包含 CJS（schemastery 内联，仅 `node:` 内置外置）——部署目录没有 node_modules 也能跑。打包产物随仓库提交，安装无需 node 工具链。

对源提示词做一次格式归一化：若 `system-role.md` 来自 GLM ChatML 导出，外层 `<|im_start|>system:` / `<|im_end|>` 传输标记会被剥离，写入的是 prompt 主体。

## 安装

```bash
python3 deepseek-keysmith.py install --dry-run   # 先看写入计划
python3 deepseek-keysmith.py install --yes        # 确认后写入
python3 deepseek-keysmith.py doctor               # 检查状态
python3 deepseek-keysmith.py verify               # 检查链路
```

安装器会备份已有受管理文件为 `<filename>.bak_YYYYMMDD_HHMMSS`。`--dry-run` 优先级最高，即使同时传 `install --yes --dry-run` 也只预览。

**已经打开的 dsh 进程需要重启**才会加载新的 home 层补丁。完成安装后验证：

1. 退出 dsh，重新启动（`pnpm dsh web` 或 `dsh --profile web`）；
2. 新开一个 agent 会话，观察 system prompt 是否包含你的 system-role 内容；
3. 终端运行 `python3 deepseek-keysmith.py verify`，确认 `patch_row_present: true` 与 `plugin_syntax: ok`。

## Harness home 不在默认路径

```bash
python3 deepseek-keysmith.py install --dsh-home /path/to/dsh-home --yes
# 或设置环境变量
DSH_HOME=/path/to/dsh-home python3 deepseek-keysmith.py install --yes
```

## 卸载

```bash
python3 deepseek-keysmith.py uninstall --dry-run   # 预览
python3 deepseek-keysmith.py uninstall --yes        # 确认移除
```

卸载会备份受管理文件、从 home 层补丁中移除 keysmith 行（保留你手写的其他行），若 home 层补丁只剩 keysmith 内容则整文件备份。Harness 原包保持完整。

## 命令

| 命令 | 作用 |
|---|---|
| `install` | 安装受管理 system-role 入口（写入 home 层补丁 + 文件） |
| `doctor`  | 报告安装状态（文件、哈希、patch 行、密钥脱敏） |
| `verify`  | 本地链路检查（patch 行、node --check 语法、源与 managed 一致性） |
| `uninstall` | 备份受管理文件并移除 patch 行 |

## 项目结构

```text
deepseek-keysmith/
├── deepseek-keysmith.py        # 单文件 CLI（零第三方依赖）
├── system-role.md              # 源 system prompt（带 ChatML 包装也支持）
├── plugin/                     # 插件本体（@deepseek-ai/dsh-keysmith）
│   ├── src/index.ts            # TypeScript 插件（真 Schemastery schema）
│   ├── package.json
│   ├── tsconfig.json
│   ├── build.mjs               # esbuild 打包成单文件自包含 bundle
│   └── dist/plugin.cjs         # 打包产物（随仓库提交，安装无需 node）
└── tests/
    └── test_deepseek_keysmith.py
```

```bash
# 重新构建插件产物（改了 plugin/src 后）
cd plugin && npm install && npm run build:bundle

# 验证
python3 -m py_compile deepseek-keysmith.py
python3 -m pytest tests/ -q
```

测试覆盖：ChatML 归一化、DSH_HOME 解析优先级、patch 行渲染（insert + file:// URL + !!js 表达式）、插件 bundle 自包含性与真 Schemastery schema 校验、dry-run 不写盘、安装写盘、重复安装不重复、保留用户已有 patch 行、doctor 密钥脱敏、verify 链路、卸载与卸载保留外部行、自定义子目录。

> 集成测试（真实 `boot()` 挂载 + 真实模型端到端会话）依赖 deepseek-harness 仓库，位于该仓库的 `keysmith/tests/`（`test-real-boot.mjs`、`test-real-model.mjs`），未包含在本仓库。

## 与系列的关系

同系列：

- [codex-keysmith](https://github.com/Jia-Ethan/codex-keysmith) - Codex CLI 本地配置的版本化指令部署工具。
- [claude-keysmith](https://github.com/Jia-Ethan/claude-keysmith) - Claude Code `CLAUDE.md` 的受管理 import-block 安装器。
- [grok-keysmith](https://github.com/Jia-Ethan/grok-keysmith) - Grok Build 的全局 `AGENTS.md` 指令部署工具。
- [zcode-keysmith](https://github.com/Jia-Ethan/zcode-keysmith) - ZCode App 的受管理 true system-role 入口，通过 agent-server wrapper 接管 runtime。
- **deepseek-keysmith**（本仓库） - DeepSeek Harness 的原生补丁层 + 一等公民插件：不改 bundle、不写依赖，system-role 走 system 消息路径。
