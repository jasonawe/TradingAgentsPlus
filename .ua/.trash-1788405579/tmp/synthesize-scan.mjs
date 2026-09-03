import { readFileSync, writeFileSync } from 'node:fs';

const root = '/Users/shenkang/workroom/languages/agent/TradingAgents';
const scan = JSON.parse(readFileSync(`${root}/.ua/tmp/ua-scan-files.json`, 'utf8'));
const imports = JSON.parse(readFileSync(`${root}/.ua/tmp/ua-import-map-output.json`, 'utf8'));
const byLanguage = Object.keys(scan.stats?.byLanguage ?? {}).sort();
const description = 'TradingAgents is a multi-agent LLM financial trading framework for research, combining specialized analysts, researchers, traders, and risk management agents. Note: this project has over 100 source files; consider scoping analysis to a subdirectory for faster results.';
const result = {
  name: 'tradingagents',
  description,
  languages: byLanguage,
  frameworks: ['Docker', 'Docker Compose', 'GitHub Actions', 'Pytest'],
  files: scan.files,
  totalFiles: scan.totalFiles,
  filteredByIgnore: scan.filteredByIgnore,
  estimatedComplexity: scan.estimatedComplexity,
  importMap: imports.importMap,
};
if (result.totalFiles !== result.files.length) {
  throw new Error(`totalFiles mismatch: ${result.totalFiles} vs ${result.files.length}`);
}
writeFileSync(`${root}/.ua/intermediate/scan-result.json`, `${JSON.stringify(result, null, 2)}\n`);
