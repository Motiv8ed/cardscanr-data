#!/usr/bin/env node

/** Repair narrowly recognized upstream TCGdex module defects without editing the mirror. */

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream, readFileSync, renameSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { createInterface } from "node:readline";
import { finished } from "node:stream/promises";
import { pathToFileURL } from "node:url";

function argumentsMap(): Map<string, string> {
  const values = new Map<string, string>();
  for (let index = 2; index < process.argv.length; index += 2) {
    const key = process.argv[index];
    const value = process.argv[index + 1];
    if (!key?.startsWith("--") || !value) throw new Error("Expected --source-root, --input, and --output");
    values.set(key.slice(2), value);
  }
  return values;
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function checkedSourcePath(sourceRoot: string, sourcePath: string): string {
  if (isAbsolute(sourcePath)) throw new Error(`Absolute source path rejected: ${sourcePath}`);
  const path = resolve(sourceRoot, sourcePath.split("/").join(sep));
  const within = relative(sourceRoot, path);
  if (within.startsWith("..") || isAbsolute(within)) throw new Error(`Source path escapes root: ${sourcePath}`);
  return path;
}

async function repairBrokenDotSetImport(record: Record<string, unknown>, sourceRoot: string) {
  const sourcePath = String(record.source_path);
  const path = checkedSourcePath(sourceRoot, sourcePath);
  const source = readFileSync(path);
  const sourceText = source.toString("utf8");
  if (!sourceText.match(/^import Set from ["']\.["']\s*$/m)) return null;
  if (!String(record.error).includes("Cannot find module './'")) return null;

  const cardDirectory = dirname(path);
  const setName = cardDirectory.slice(cardDirectory.lastIndexOf(sep) + 1);
  const setPath = join(dirname(cardDirectory), `${setName}.ts`);
  const setModule = await import(`${pathToFileURL(setPath).href}?sha256=${sha256(readFileSync(setPath))}`);
  const executable = sourceText
    .replace(/^import Set from ["']\.["']\s*$/m, "")
    .replace(/^import \{ Card \} from ["'][^"']+["']\s*$/m, "")
    .replace(/const card\s*:\s*Card\s*=/, "const card =")
    .replace(/export default card\s*$/m, "return card");
  const payload = new Function("Set", `"use strict";\n${executable}`)(setModule.default);
  const { error: originalError, ...withoutError } = record;
  return {
    ...withoutError,
    payload,
    recovery: {
      method: "broken_dot_set_import_to_sibling_set_module",
      original_error: originalError,
      set_source_path: relative(sourceRoot, setPath).split(sep).join("/"),
      repair_tool_schema_version: 1,
    },
  };
}

async function main() {
  const values = argumentsMap();
  for (const key of ["source-root", "input", "output"]) {
    if (!values.has(key)) throw new Error(`Missing --${key}`);
  }
  const sourceRoot = resolve(values.get("source-root")!);
  const input = resolve(values.get("input")!);
  const output = resolve(values.get("output")!);
  if (input === output) throw new Error("Input and output must differ; raw exports are immutable");
  const temporary = `${output}.tmp`;
  const writer = createWriteStream(temporary, { encoding: "utf8", flags: "wx" });
  const reader = createInterface({ input: createReadStream(input, { encoding: "utf8" }), crlfDelay: Infinity });
  let records = 0;
  let inputErrors = 0;
  let repaired = 0;
  let remainingErrors = 0;
  for await (const line of reader) {
    records += 1;
    let record = JSON.parse(line) as Record<string, unknown>;
    if (Object.hasOwn(record, "error")) {
      inputErrors += 1;
      record = await repairBrokenDotSetImport(record, sourceRoot) ?? record;
      if (Object.hasOwn(record, "error")) remainingErrors += 1;
      else repaired += 1;
    }
    if (!writer.write(`${JSON.stringify(record)}\n`)) {
      await new Promise<void>((resolveDrain) => writer.once("drain", resolveDrain));
    }
  }
  writer.end();
  await finished(writer);
  renameSync(temporary, output);
  process.stdout.write(`${JSON.stringify({ records, input_errors: inputErrors, repaired, remaining_errors: remainingErrors, output })}\n`);
  if (remainingErrors) process.exitCode = 2;
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exitCode = 1;
});
