const fs = require("fs");

const scanPath = process.argv[2];
const importPath = process.argv[3];
const outputPath = process.argv[4];

const scan = JSON.parse(fs.readFileSync(scanPath, "utf8"));
const imports = JSON.parse(fs.readFileSync(importPath, "utf8"));

const languages = Object.keys(scan.stats.byLanguage).sort((a, b) => a.localeCompare(b));
const result = {
  name: "tradingagents",
  description:
    "TradingAgents 是一个由多智能体与大语言模型驱动的金融交易框架。注意：该项目包含超过 100 个源文件；如需更快的分析，建议将范围限定到某个子目录。",
  languages,
  frameworks: ["FastAPI", "Uvicorn", "Pytest", "Docker", "Docker Compose", "GitHub Actions"],
  files: scan.files,
  totalFiles: scan.totalFiles,
  filteredByIgnore: scan.filteredByIgnore,
  estimatedComplexity: scan.estimatedComplexity,
  importMap: imports.importMap,
};

fs.writeFileSync(outputPath, `${JSON.stringify(result, null, 2)}\n`);
