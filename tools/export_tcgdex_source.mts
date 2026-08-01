#!/usr/bin/env node

/**
 * Resumable, deterministic exporter for the pinned TCGdex TypeScript database.
 *
 * TCGdex stores one ES module per series, set, and card.  This tool evaluates
 * those modules with tsx and emits UTF-8 JSONL while retaining the source path,
 * source bytes checksum, and import errors.  The JSONL is an immutable staging
 * input; normalization happens in Python so it can be unit tested separately.
 */

import { createHash } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { readdir } from "node:fs/promises";
import { basename, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

type Checkpoint = {
  schema_version: 1;
  source_root: string;
  file_list_sha256: string;
  next_index: number;
  exported: number;
  errors: number;
  complete: boolean;
};

function parseArgs(): { sourceRoot: string; output: string; checkpoint: string; maxFiles: number } {
  const values = new Map<string, string>();
  for (let index = 2; index < process.argv.length; index += 2) {
    const key = process.argv[index];
    const value = process.argv[index + 1];
    if (!key?.startsWith("--") || !value) {
      throw new Error("Usage: export_tcgdex_source.mts --source-root PATH --output FILE --checkpoint FILE");
    }
    values.set(key.slice(2), value);
  }
  for (const required of ["source-root", "output", "checkpoint"]) {
    if (!values.has(required)) throw new Error(`Missing --${required}`);
  }
  return {
    sourceRoot: resolve(values.get("source-root")!),
    output: resolve(values.get("output")!),
    checkpoint: resolve(values.get("checkpoint")!),
    maxFiles: Number.parseInt(values.get("max-files") ?? "8000", 10),
  };
}

async function walk(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return walk(path);
    return entry.isFile() && entry.name.endsWith(".ts") ? [path] : [];
  }));
  return nested.flat();
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function atomicJson(path: string, value: unknown): void {
  const temporary = `${path}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  renameSync(temporary, path);
}

function classify(sourceRoot: string, path: string) {
  const source_path = relative(sourceRoot, path).split(sep).join("/");
  const parts = source_path.split("/");
  const source_domain = parts[0] === "data-asia" ? "asia" : "international";
  const depth = parts.length - 1;
  const record_type = depth === 1 ? "series" : depth === 2 ? "set" : "card";
  const provider_record_id = source_path.replace(/\.ts$/, "");
  return { source_path, source_domain, record_type, provider_record_id };
}

async function exportOne(sourceRoot: string, path: string, index: number) {
  const source = readFileSync(path);
  const identity = classify(sourceRoot, path);
  try {
    // The checksum query makes the module cache key content-addressed.
    const url = `${pathToFileURL(path).href}?sha256=${sha256(source)}`;
    const module = await import(url);
    return {
      schema_version: 1,
      index,
      ...identity,
      source_byte_size: statSync(path).size,
      source_sha256: sha256(source),
      payload: module.default,
    };
  } catch (error) {
    return {
      schema_version: 1,
      index,
      ...identity,
      source_byte_size: statSync(path).size,
      source_sha256: sha256(source),
      error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
    };
  }
}

async function main() {
  const args = parseArgs();
  const roots = [join(args.sourceRoot, "data"), join(args.sourceRoot, "data-asia")];
  const files = (await Promise.all(roots.map(walk))).flat()
    .sort((left, right) => left.localeCompare(right, "en"));
  const fileListSha256 = sha256(files.map((path) => relative(args.sourceRoot, path).split(sep).join("/")).join("\n"));

  let checkpoint: Checkpoint = {
    schema_version: 1,
    source_root: args.sourceRoot,
    file_list_sha256: fileListSha256,
    next_index: 0,
    exported: 0,
    errors: 0,
    complete: false,
  };
  if (existsSync(args.checkpoint)) {
    checkpoint = JSON.parse(readFileSync(args.checkpoint, "utf8")) as Checkpoint;
    if (checkpoint.source_root !== args.sourceRoot || checkpoint.file_list_sha256 !== fileListSha256) {
      throw new Error("Checkpoint belongs to a different source tree or file list");
    }
    if (!existsSync(args.output) && checkpoint.next_index > 0) {
      throw new Error("Checkpoint has progress but output JSONL is missing");
    }
  } else if (existsSync(args.output)) {
    throw new Error("Output exists without a checkpoint; move it aside or supply its checkpoint");
  }
  if (checkpoint.complete) {
    process.stdout.write(`${JSON.stringify({ status: "already_complete", files: files.length, ...checkpoint })}\n`);
    return;
  }

  const batchSize = 24;
  let processedThisRun = 0;
  for (let start = checkpoint.next_index; start < files.length; start += batchSize) {
    const remainingThisRun = args.maxFiles - processedThisRun;
    if (remainingThisRun <= 0) break;
    const end = Math.min(files.length, start + batchSize, start + remainingThisRun);
    const records = await Promise.all(files.slice(start, end).map((path, offset) =>
      exportOne(args.sourceRoot, path, start + offset)));
    appendFileSync(args.output, records.map((record) => JSON.stringify(record)).join("\n") + "\n", "utf8");
    checkpoint.next_index = end;
    checkpoint.exported += records.filter((record) => !("error" in record)).length;
    checkpoint.errors += records.filter((record) => "error" in record).length;
    checkpoint.complete = end === files.length;
    processedThisRun += records.length;
    atomicJson(args.checkpoint, checkpoint);
    if (end % 1000 < batchSize || end === files.length) {
      process.stdout.write(`${JSON.stringify({ status: "running", files: files.length, ...checkpoint })}\n`);
    }
  }
  if (!checkpoint.complete) {
    process.stdout.write(`${JSON.stringify({ status: "checkpoint_pause", files: files.length, processed_this_run: processedThisRun, ...checkpoint })}\n`);
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exitCode = 1;
});
