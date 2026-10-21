# 更新 GitHub 仓库到 v1.3.2

1. 打开已有仓库 https://github.com/xsisme/mail-forwarder-client ，点击 Add file → Upload files。
2. 解压 `MailForwarderClient_v1.3.2_Windows.zip`，打开里面的 `MailForwarderClient` 文件夹。
3. 把文件夹内的源码文件及 `mailclient`、`tests` 文件夹拖入网页，保持目录结构。上传后 `README.md`、`app.py` 应在仓库根目录，不要多套一层 `MailForwarderClient`，也不要把 ZIP 当源码上传。
4. 确认新文件 `mailclient/cancellation.py`、`tests/test_exclusions.py` 和 `tests/test_shutdown.py` 已包含，已有文件会按同路径更新。
5. `.gitignore` 可能被文件选择器隐藏，请确认它也上传；必要时用 Add file → Create new file 单独创建。网页上传不会应用忽略规则，请从刚解压的干净包上传，避免混入运行后的配置、日志或构建目录。
6. 提交说明可填 `Fix Windows blocked-send test synchronization (v1.3.2)`，检查变更后点击 Commit changes。

本包以公开仓库为基础，默认 POP3 主机、邮箱账号、排除名单及凭据均为空，示例只使用保留示例域名。旧客户端在本机保存的配置和转发记录仍可沿用。没有预编译 Windows EXE；在 Windows 上执行 `build_windows.bat` 生成新版。
