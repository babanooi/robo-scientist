import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/dxm/roboscientist/outputs/2026-07-14-member-progress/机械臂AI_Scientist_成员任务进度表.xlsx";
const outputPath = inputPath;
const previewDir = "/Users/dxm/roboscientist/outputs/2026-07-15-xlsx-update/previews";

const blob = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(blob);

const overview = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 12000,
  tableMaxRows: 12,
  tableMaxCols: 10,
  tableMaxCellChars: 120,
});
console.log(overview.ndjson);

const overviewSheet = workbook.worksheets.getItem("总览");
overviewSheet.getRange("A2").values = [["计划周期：2026-07-14 至 2026-08-31。最后更新：2026-07-15。请各成员在个人工作表更新状态、实际完成日及证据/阻塞；总览会自动汇总阶段状态与完成率。"]];
overviewSheet.getRange("E6:F6").values = [["研究生A-设备接口/部署", "设备接口、ROS2、部署链路与运行环境"]];

const captainSheet = workbook.worksheets.getItem("队长-上层智能体");
captainSheet.getRange("C5:D5").values = [[
  "冻结 MVP、成功标准、指标、Schema、项目骨架和本地/实机部署方案",
  "ExperimentPlan / ExperimentResult / 错误码 / 适配器 Schema；Mock 服务骨架；本地电脑与树莓派的部署拓扑说明",
]];

const aSheet = workbook.worksheets.getItem("研究生A-设备接口");
aSheet.getRange("A1").values = [["研究生A-设备接口、ROS2 与部署链路｜阶段任务进度表"]];
aSheet.getRange("A2").values = [["主责：设备接口、ROS2、部署链路与运行环境。每周一更新状态、实际完成日与证据/阻塞；完成前需由交接对象验收。"]];
aSheet.getRange("C5:E5").values = [[
  "调查 ROS2、SDK、节点、服务、官方示例并跑通环境；确认 SSH/Git/Remote-SSH/scp 或 rsync 代码导入方式",
  "启动命令、依赖配置、ROS 接口清单、官方例程记录、树莓派 IP/账号/项目目录/同步命令/日志路径",
  "队长冻结适配器；能把最小脚本同步到机械臂并运行",
]];
aSheet.getRange("C6:D6").values = [[
  "封装回零、状态、移动、夹爪和停止接口；建立树莓派侧 robot_adapter 最小入口",
  "ArmAdapter 方法签名、结构化返回、错误码、调用示例、部署后启动/停止/查看日志命令",
]];
aSheet.getRange("C7:D7").values = [[
  "对齐 Mock/实机接口，补齐超时、状态、配置切换和部署环境变量",
  "接口差异清单、配置切换方式、已知限制、实机部署目录结构",
]];
aSheet.getRange("C8:D8").values = [[
  "完成真实适配器、启动/停止/恢复、部署自检与动作事件日志",
  "可调用实机适配器；动作时间、最终状态、错误码；网络/ROS2/硬件三类自检结果",
]];
aSheet.getRange("C10:D10").values = [[
  "固化现场启动、停止、部署自检和故障处理",
  "一键启动/停止说明、故障排查表、部署自检步骤",
]];
aSheet.getRange("D11:E11").values = [[
  "环境版本、设备配置、启动/恢复手册、部署复现说明",
  "非 A 成员可按文档完成准备、部署自检与安全停止",
]];

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  maxChars: 4000,
});
console.log(errors.ndjson);

await fs.mkdir(previewDir, { recursive: true });
const sheetOverview = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 8000 });
const sheetRecords = sheetOverview.ndjson
  .trim()
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line));

for (const record of sheetRecords) {
  const name = record.name;
  try {
    const preview = await workbook.render({ sheetName: name, autoCrop: "all", scale: 1, format: "png" });
    const bytes = new Uint8Array(await preview.arrayBuffer());
    await fs.writeFile(`${previewDir}/${name}.png`, bytes);
  } catch (error) {
    console.log(`render skipped for ${name}: ${error.message}`);
  }
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
