# Waymark Travel Agent

输入城市名，按小红书原帖整理玩法、餐厅、住宿、交通和避坑信息。中国内地的餐厅候选来自高德和大众点评，海外候选按地区选择来源，再核对 Google Maps 的评分和评论数。每条入选结论保留来源。

## 代码入口

- [Codex 智能体定义](agents/xhs-travel-researcher.toml)
- [小红书检索与提取规则](skills/xhs-travel-notes/SKILL.md)和 [Mac 采集入口](skills/xhs-travel-notes/scripts/collect_city.py)
- [目的地核验服务](destination-service/README.md)和[地区来源配置](Waymark/DestinationSources.json)
- Waymark 通过 `waymark-plan.json` 导入 Mac 搜索结果。iPhone App 在独立项目中维护。

## 在 Mac 上使用

使用 Python 3.12 或更新版本，并安装 Node.js。在线小红书采集使用单独安装的 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)，本版本对接的提交是 `09ed1ec62d885dab422a53de999f52ac0bd4966b`。使用上游项目的安装说明准备依赖和浏览器。将 `MEDIACRAWLER_HOME` 指向上游源码目录，并用装有对应依赖的 Python 执行。已有 Waymark 环境可直接运行以下命令。

```sh
"$HOME/Library/Application Support/Waymark/MediaCrawler/venv/bin/python" \
  skills/xhs-travel-notes/scripts/collect_city.py 昆明 --topic 美食 --output output/昆明
```

每次运行使用独立结果目录。平台要求登录时，在采集窗口完成登录。可选的模型提取和海外查询环境变量见各自的 skill 和服务说明。

## 本地验证

在仓库根目录运行，离线测试使用 Node.js 和 Python 标准库。

```sh
python3 -m unittest discover -s skills/xhs-travel-notes/scripts
python3 -m unittest discover -s destination-service
```

城市检索数据节选自 Waymark 的地名库，包含经过筛选和名称归一化的 [GeoNames](https://www.geonames.org/export/) 数据（[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)），以及 AreaCity 行政区划名称。小樽的公开地点快照用于回归测试。
