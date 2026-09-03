const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const projectRoot = process.argv[2];
const uaDir = path.join(projectRoot, ".ua");
const scan = JSON.parse(
  fs.readFileSync(path.join(uaDir, "intermediate", "scan-result.json"), "utf8"),
);
const gitCommitHash = execFileSync("git", ["rev-parse", "HEAD"], {
  cwd: projectRoot,
  encoding: "utf8",
}).trim();

fs.writeFileSync(
  path.join(uaDir, "meta.json"),
  JSON.stringify(
    {
      lastAnalyzedAt: new Date().toISOString(),
      gitCommitHash,
      version: "1.0.0",
      analyzedFiles: scan.files.length,
    },
    null,
    2,
  ) + "\n",
);

console.log(`Wrote graph metadata for ${scan.files.length} files at ${gitCommitHash}`);
