import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const path = "/Users/dxm/roboscientist/outputs/2026-07-14-member-progress/机械臂AI_Scientist_成员任务进度表.xlsx";
const blob = await FileBlob.load(path);
const workbook = await SpreadsheetFile.importXlsx(blob);

for (const [sheetName, range] of [
  ["总览", "A2:F6"],
  ["队长-上层智能体", "C5:D5"],
  ["研究生A-设备接口", "A1:E11"],
]) {
  const inspected = await workbook.inspect({
    kind: "table",
    sheetId: sheetName,
    range,
    include: "values,formulas",
    tableMaxRows: 12,
    tableMaxCols: 8,
    tableMaxCellChars: 160,
    maxChars: 12000,
  });
  console.log(inspected.ndjson);
}

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  maxChars: 4000,
});
console.log(errors.ndjson);
