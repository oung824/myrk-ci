# Myrk Windows CI

本仓库保存一份 Myrk 源码快照，用于在不同的 Windows 托管机上验证安装和离线测试入口。

在 GitHub Actions 手动运行 **Windows offline pilot**。试跑包含 Python 类型检查、TypeScript 类型检查、两组后端测试、桌面测试入口契约和 Office 安装探测。固定执行器测试使用 Windows 11 ARM。后端测试使用固定样例和本地替身。准备依赖时需要联网下载公开的软件包。

**Windows public offline backend** 将 27 份已选后端测试文件分到六台托管机，并比对发现和实际执行的用例身份。需要真实文件访问限制的布局测试使用 Windows 11 ARM。**Windows 11 ARM executor probe** 用同一固定执行器探测该环境的系统访问限制能力。

**Windows public offline backend remainder** 将其余 45 份顶层后端测试文件分配给 13 台 Windows 托管机，按文件报告通过、失败和回收结果。

**Windows public offline judge** 验证 130 项本地合成账本和两次固定回复的 SDK 调用。试跑用空历史索引生成本轮报告，历史原件专项在本机运行。

**Windows public offline desktop** 在四台 Windows 11 ARM 托管机上构建 Electron 并运行桌面行为测试。三个作业执行普通和串行测试文件，一个作业执行四项单独选出的 PDF 与编程用例。Office 文档预览专项由本机负责。

试跑范围以工作流列出的用例为准。完整验收还需要独立覆盖全部后端和桌面用例，并用装有桌面 Office 的受控 Windows 环境验证文档预览。包含本机验收记录的历史资料留在原项目内。公开工作流的执行日志应只保留试跑范围、耗时和状态。
