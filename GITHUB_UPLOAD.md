# 上传 GitHub

1. 登录 https://github.com/new ，仓库名建议 mail-forwarder-client，描述：Windows desktop POP3 mail forwarder with WeCom and SMTP delivery, encrypted credentials and multi-instance support.
2. 选择 Public，勾选 Add a README file 后创建仓库。不要另选许可证，本包已含 MIT LICENSE。
3. 进入仓库，点击 Add file → Upload files。
4. 解压公开包，打开 MailForwarderClient_GitHub 文件夹，把里面的文件与 mailclient、tests 文件夹拖入页面，保持目录结构。不要上传 ZIP 本身。README.md 应在仓库根目录。
5. .gitignore 可能被文件选择器隐藏，请确认它也上传；网页上传不会应用忽略规则。只能使用本公开包，不要混入运行后的配置或日志。
6. 填写提交说明 Initial public release，然后 Commit changes。
7. 检查根目录存在 README.md、LICENSE、app.py、mailclient/、tests/。

本包只修改公开版默认 POP3 主机为空，不更改既有客户端功能。没有预编译 Windows EXE；本机打包请使用 build_windows.bat。
