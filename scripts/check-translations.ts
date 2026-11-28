import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Configuration
const LOCALES_DIR = path.join(__dirname, "..", "src", "i18n", "locales");
const REFERENCE_LANG = "en";

type TranslationData = Record<string, unknown>;

interface ValidationResult {
  valid: boolean;
  missing: string[][];
  extra: string[][];
  issues: string[];
}

function getLanguages(): string[] {
  const entries = fs.readdirSync(LOCALES_DIR, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isDirectory() && entry.name !== REFERENCE_LANG)
    .map((entry) => entry.name)
    .sort();
}

const LANGUAGES = getLanguages();

// Colors for terminal output
const colors: Record<string, string> = {
  reset: "\x1b[0m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  blue: "\x1b[34m",
};

function colorize(text: string, color: string): string {
  return `${colors[color]}${text}${colors.reset}`;
}

function getAllKeyPaths(
  obj: TranslationData,
  prefix: string[] = [],
): string[][] {
  let paths: string[][] = [];
  for (const key in obj) {
    if (!Object.hasOwn(obj, key)) continue;

    const currentPath = prefix.concat([key]);
    const value = obj[key];

    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      paths = paths.concat(
        getAllKeyPaths(value as TranslationData, currentPath),
      );
    } else {
      paths.push(currentPath);
    }
  }
  return paths;
}

function hasKeyPath(obj: TranslationData, keyPath: string[]): boolean {
  let current: unknown = obj;
  for (const key of keyPath) {
    if (
      typeof current !== "object" ||
      current === null ||
      (current as Record<string, unknown>)[key] === undefined
    ) {
      return false;
    }
    current = (current as Record<string, unknown>)[key];
  }
  return true;
}

function getNestedValue(obj: TranslationData, keyPath: string[]): unknown {
  let current: unknown = obj;
  for (const key of keyPath) {
    if (typeof current !== "object" || current === null) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

const SHARED_CUSTOM_WORDS_KEYS = [
  "synced",
  "pending",
  "unsupported",
  "failed",
] as const;

const PLACEHOLDER_RE = /\{\{[^}]+\}\}/g;
const CJK_RE = /[\u4e00-\u9fff]/;

function extractPlaceholders(text: string): string[] {
  return (text.match(PLACEHOLDER_RE) ?? []).slice().sort();
}

// Fork: guard settings.advanced.customWords.shared against ANSI '?' encoding
// corruption (e.g. CJK -> '???' or '…' -> '?') and placeholder drift without
// forbidding legitimate question marks in other translation keys.
function checkSharedCustomWords(
  lang: string,
  data: TranslationData,
  referenceData: TranslationData,
): string[] {
  const issues: string[] = [];
  for (const key of SHARED_CUSTOM_WORDS_KEYS) {
    const keyPath = ["settings", "advanced", "customWords", "shared", key];
    const label = keyPath.join(".");
    const value = getNestedValue(data, keyPath);
    const refValue = getNestedValue(referenceData, keyPath);
    if (typeof value !== "string" || value.trim().length === 0) {
      issues.push(`${label}: expected a non-empty string`);
      continue;
    }
    if (value.includes("?") || value.includes("\uFFFD")) {
      issues.push(
        `${label}: contains corrupted '?' or replacement character (${JSON.stringify(value)})`,
      );
    }
    if (key === "pending" && !value.endsWith("\u2026")) {
      issues.push(
        `${label}: must end with Unicode ellipsis '…' (${JSON.stringify(value)})`,
      );
    }
    if ((lang === "zh" || lang === "zh-TW") && !CJK_RE.test(value)) {
      issues.push(`${label}: expected CJK characters for ${lang}`);
    }
    if (typeof refValue === "string") {
      const expected = extractPlaceholders(refValue);
      const actual = extractPlaceholders(value);
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        issues.push(
          `${label}: placeholder mismatch (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`,
        );
      }
    }
  }
  return issues;
}

function loadTranslationFile(lang: string): TranslationData | null {
  const filePath = path.join(LOCALES_DIR, lang, "translation.json");

  try {
    const raw = fs.readFileSync(filePath);
    if (
      raw.length >= 3 &&
      raw[0] === 0xef &&
      raw[1] === 0xbb &&
      raw[2] === 0xbf
    ) {
      throw new Error("UTF-8 BOM is not allowed");
    }
    const content = new TextDecoder("utf-8", { fatal: true }).decode(raw);
    return JSON.parse(content) as TranslationData;
  } catch (error) {
    console.error(colorize(`✗ Error loading ${lang}/translation.json:`, "red"));
    console.error(`  ${(error as Error).message}`);
    return null;
  }
}

function validateTranslations(): void {
  console.log(colorize("\n🌍 Translation Consistency Check\n", "blue"));

  // Load reference file
  console.log(`Loading reference language: ${REFERENCE_LANG}`);
  const referenceData = loadTranslationFile(REFERENCE_LANG);

  if (!referenceData) {
    console.error(
      colorize(`\n✗ Failed to load reference file (${REFERENCE_LANG})`, "red"),
    );
    process.exit(1);
  }

  // Get all key paths from reference
  const referenceKeyPaths = getAllKeyPaths(referenceData);
  console.log(`Reference has ${referenceKeyPaths.length} keys\n`);

  // Track validation results
  let hasErrors = false;
  const results: Record<string, ValidationResult> = {};

  const referenceIssues = checkSharedCustomWords(
    REFERENCE_LANG,
    referenceData,
    referenceData,
  );
  if (referenceIssues.length > 0) {
    hasErrors = true;
    console.log(
      colorize(`✗ ${REFERENCE_LANG.toUpperCase()}: Issues found`, "red"),
    );
    for (const issue of referenceIssues) {
      console.log(`    - ${issue}`);
    }
    console.log("");
  }

  // Validate each language
  for (const lang of LANGUAGES) {
    const langData = loadTranslationFile(lang);

    if (!langData) {
      hasErrors = true;
      results[lang] = {
        valid: false,
        missing: [],
        extra: [],
        issues: ["Failed to parse UTF-8 JSON without BOM"],
      };
      continue;
    }

    // Find missing keys
    const missing = referenceKeyPaths.filter(
      (keyPath) => !hasKeyPath(langData, keyPath),
    );

    // Find extra keys (keys in language but not in reference)
    const langKeyPaths = getAllKeyPaths(langData);
    const extra = langKeyPaths.filter(
      (keyPath) => !hasKeyPath(referenceData, keyPath),
    );

    const issues = checkSharedCustomWords(lang, langData, referenceData);

    results[lang] = {
      valid: missing.length === 0 && extra.length === 0 && issues.length === 0,
      missing,
      extra,
      issues,
    };

    if (missing.length > 0 || extra.length > 0 || issues.length > 0) {
      hasErrors = true;
    }
  }

  // Print results
  console.log(colorize("Results:", "blue"));
  console.log("─".repeat(60));

  for (const lang of LANGUAGES) {
    const result = results[lang];

    if (result.valid) {
      console.log(
        colorize(`✓ ${lang.toUpperCase()}: All keys present`, "green"),
      );
    } else {
      console.log(colorize(`✗ ${lang.toUpperCase()}: Issues found`, "red"));

      if (result.missing.length > 0) {
        console.log(
          colorize(`  Missing ${result.missing.length} keys:`, "yellow"),
        );
        result.missing.slice(0, 10).forEach((keyPath) => {
          console.log(`    - ${keyPath.join(".")}`);
        });
        if (result.missing.length > 10) {
          console.log(
            colorize(
              `    ... and ${result.missing.length - 10} more`,
              "yellow",
            ),
          );
        }
      }

      if (result.extra.length > 0) {
        console.log(
          colorize(
            `  Extra ${result.extra.length} keys (not in reference):`,
            "yellow",
          ),
        );
        result.extra.slice(0, 10).forEach((keyPath) => {
          console.log(`    - ${keyPath.join(".")}`);
        });
        if (result.extra.length > 10) {
          console.log(
            colorize(`    ... and ${result.extra.length - 10} more`, "yellow"),
          );
        }
      }

      if (result.issues.length > 0) {
        console.log(
          colorize(`  Quality issues (${result.issues.length}):`, "yellow"),
        );
        result.issues.forEach((issue) => {
          console.log(`    - ${issue}`);
        });
      }

      console.log("");
    }
  }

  console.log("─".repeat(60));

  // Summary
  const validCount = Object.values(results).filter((r) => r.valid).length;
  const totalCount = LANGUAGES.length;

  if (hasErrors) {
    console.log(
      colorize(
        `\n✗ Validation failed: ${validCount}/${totalCount} languages passed`,
        "red",
      ),
    );
    process.exit(1);
  } else {
    console.log(
      colorize(
        `\n✓ All ${totalCount} languages have complete translations!`,
        "green",
      ),
    );
    process.exit(0);
  }
}

// Run validation
validateTranslations();
