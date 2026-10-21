# 端到端测试

六个脚本都直接打本机正在运行的 MakiseToolkit（默认 127.0.0.1:8931），
所以**先启动应用**再跑。

    node tests/e2e/api.mjs                      # 接口层（23 项）
    node tests/e2e/ui.mjs                       # 画布层（23 项）
    node tests/e2e/header.mjs                   # 左上角按钮 + 删除项目（15 项）
    node tests/e2e/scenarios/lifecycle.mjs      # 节点与文件生命周期（86 项）
    node tests/e2e/scenarios/branch-merge.mjs   # 分支/合并/回滚/导出（82 项）
    node tests/e2e/scenarios/edge-cases.mjs     # 边界值与异常（80 项）
    node tests/e2e/buttons.mjs                  # 画布页每个按钮逐个点（41 项）
    node tests/e2e/buttons-list.mjs             # 列表页与新建立/改名/删除（17 项）
    node tests/e2e/link.mjs                     # 下载结果存进项目（联动，8 项）

约定：

* 每个脚本用独立临时项目目录和独立浏览器配置目录
* 全部通过时退出码 0，最后一行为 `结果: x/y`
* api.mjs 里有 30 秒静默窗口的自动保存测试，单个脚本约 1 分钟

## 最近一次结果（连续两轮）

    第 1 轮: API 23/23 | UI 23/23 | 头部 15/15 | 生命周期 86/86 | 分支合并 82/82 | 边界异常 80/80
    第 2 轮: API 23/23 | UI 23/23 | 头部 15/15 | 生命周期 86/86 | 分支合并 82/82 | 边界异常 80/80
