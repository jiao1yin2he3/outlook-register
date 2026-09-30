# Outlook Register

批量注册 Outlook/Hotmail 邮箱的自动化系统，基于浏览器自动化（Patchright / Playwright），支持代理、防机器人检测等待、辅助（恢复）邮箱绑定，以及免费代理池服务。

> 仅供学习与研究用途，请遵守目标服务的使用条款与当地法律法规。

## 功能特性

- **浏览器自动化注册**：支持 Patchright（反检测）与 Playwright 两种浏览器驱动
- **代理支持**：单代理 / 代理池 / 代理文件，HTTP、SOCKS5 均可
- **防机器人处理**：可配置的验证等待时间与验证码重试次数
- **辅助邮箱绑定**：注册后自动进入账户安全页，添加并验证恢复邮箱（支持多域名、多收码 API）
- **临时邮箱**：内置 mail.tm 等临时邮箱服务，用于接收验证码
- **免费代理池**：独立的代理池服务（FastAPI），支持采集、校验、分发
- **批量注册**：子进程隔离的批量任务，自动轮换代理节点、断点续跑

## 目录结构

```
├── main.py                    # 主入口（register / proxy / email / test / browser）
├── config.json                # 配置文件
├── config.py                  # 配置加载与校验
├── controllers/               # 浏览器控制器
│   ├── base_controller.py     #   注册流程核心逻辑
│   ├── patchright_controller.py
│   └── playwright_controller.py
├── recovery_binder/           # 辅助（恢复）邮箱绑定模块
│   ├── runner.py              #   绑定主循环
│   ├── observe.py             #   页面状态识别
│   ├── actions.py             #   页面操作
│   ├── codes.py               #   收码域名生成与验证码轮询
│   └── tests/                 #   单元测试
├── freeproxy/                 # 免费代理池服务
├── temp_mail_inbox/           # 临时邮箱服务
├── batch_register.py          # 批量注册（子进程隔离 + 节点轮换）
├── register_single.py         # 单次注册（批量任务的子进程单元）
├── utils.py                   # 随机邮箱 / 强密码生成
├── geo.py                     # 代理出口地理位置检测
├── proxy_pool.py              # 代理列表获取与管理
├── get_token.py               # OAuth2 token 获取（PKCE）
└── requirements.txt
```

## 环境要求

- Python 3.9+
- macOS / Linux

## 安装

```bash
pip install -r requirements.txt

# Patchright 模式（推荐）
patchright install chromium

# 或 Playwright 模式
playwright install chromium
```

## 配置说明

`config.json` 主要字段：

| 字段 | 说明 |
|------|------|
| `choose_browser` | `patchright`（推荐，反检测）或 `playwright`（需填 `playwright.browser_path`） |
| `email_suffix` | `@outlook.com` 或 `@hotmail.com` |
| `proxy_mode` | `single` 单代理 / `pool` 代理池 / `file` 代理文件 |
| `proxy` | 代理地址，如 `http://127.0.0.1:18080`、`socks5://user:pass@host:port` |
| `headless` | 是否无头模式（部分风控场景建议关闭） |
| `bot_protection_wait` | 遇到机器人验证时的等待秒数 |
| `max_captcha_retries` | 验证码重试次数 |
| `bind_enabled` | 注册成功后是否自动绑定辅助邮箱 |
| `temp_email.provider` | 临时邮箱服务（`mailtm` 等） |
| `oauth2` | 可选，启用后注册完成会获取 OAuth2 token |

辅助邮箱绑定可在配置中通过 `bind_*` 键覆盖默认值，常用项：

| 键 | 默认值 | 说明 |
|----|--------|------|
| `bind_target_count` | `2` | 绑定的恢复邮箱数量 |
| `bind_timeout_sec` | `180` | 绑定流程总超时 |
| `bind_mail_source` | `random` | 收码来源：`cf` / `legacy` / `random` |
| `bind_domains` | 见 `recovery_binder/config.py` | legacy 收码域名及 API |
| `bind_cf_api_base` | 见 `recovery_binder/config.py` | CF 收码服务地址 |
| `bind_cf_domains` | 见 `recovery_binder/config.py` | CF 可选域名列表 |
| `bind_debug` | `false` | 失败时 dump 页面快照 |

> 默认的收码域名与 API 为占位符，请替换为你自己的收码服务后使用。

## 使用方法

```bash
# 单次注册
python main.py register

# 浏览器连通性测试
python main.py browser

# 临时邮箱测试
python main.py email

# 组件综合测试
python main.py test

# 启动免费代理池服务
python main.py proxy
```

### 批量注册

批量任务会轮换代理节点，每个账号在独立子进程中执行，进度保存在 `Results/batch_status.json`，中断后重跑会自动续上：

```bash
# 修改 batch_register.py 顶部的 TARGET_COUNT / PROXY_NODES 后：
python3 batch_register.py
```

批量模式依赖一个本地 Clash/mihomo 代理（`config.json` 中的 `proxy`），并通过其 HTTP API（默认 `127.0.0.1:19099`）切换节点。

单次调用（批量任务的子进程单元）：

```bash
python3 register_single.py
```

## 运行测试

```bash
python -m unittest discover -s recovery_binder/tests
```

## 结果输出

- `Results/accounts.txt` — 注册成功的账号（时间、邮箱、密码、确认状态）
- `Results/batch_status.json` — 批量任务进度
- `Results/all-recovery-emails.csv` — 绑定的辅助邮箱记录
- `Results/bind-status.csv` — 绑定结果状态
- `Results/logs/` — 运行日志

## 免责声明

本项目仅用于学习与技术研究。使用者应自行承担因滥用本项目产生的一切责任与后果。
