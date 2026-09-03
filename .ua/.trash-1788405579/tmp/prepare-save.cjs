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

fs.copyFileSync(
  path.join(uaDir, "intermediate", "assembled-graph.json"),
  path.join(uaDir, "knowledge-graph.json"),
);
fs.writeFileSync(
  path.join(uaDir, "intermediate", "fingerprint-input.json"),
  JSON.stringify(
    {
      projectRoot,
      sourceFilePaths: scan.files.map((file) => file.path),
      gitCommitHash,
    },
    null,
    2,
  ),
);

console.log(`Prepared graph and ${scan.files.length} fingerprint inputs at ${gitCommitHash}`);
