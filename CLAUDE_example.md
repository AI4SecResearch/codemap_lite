# ark-ghidra — Ghidra ArkTS Decompiler Plugin

## Project Overview

A Ghidra plugin that provides native decompilation support for ArkTS (the language used by HarmonyOS/OpenHarmony applications).

**Tech stack:** Java 21 + Gradle + Ghidra 12.0.4 Extension API

**Repository:** https://github.com/miaochiahao/ark-ghidra

---

## Architecture

```
ark-ghidra/
├── CLAUDE.md              # This file — project rules & context
├── README.md              # User-facing documentation
├── build.gradle           # Gradle build with Ghidra extension support
├── src/
│   ├── main/java/         # Plugin source code
│   └── test/java/         # Unit and integration tests
├── data/                  # Test .abc binaries and fixtures
└── ghidra_scripts/        # Ghidra scripting bridge (optional)
```

### Core components

1. **ABC Loader** — Parses `.abc` files, maps sections into Ghidra address spaces
2. **Bytecode Decoder** — Disassembles ~220 Ark bytecode instructions
3. **SLEIGH Processor Module** — Instruction encoding/disassembly
4. **ArkTS Decompiler** — CFG → AST → ArkTS source code
5. **Auto-Analyzer** — Ghidra analysis pipeline integration
6. **UI Plugin** — Menu actions, structure viewer, decompiler output panel

---

## Development Environment

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
export PATH="$JAVA_HOME/bin:$PATH"

./gradlew build                  # Compile plugin
./gradlew test                   # Run tests
./gradlew lint                   # Run static analysis
```

### E2E Headless Decompilation Test

Full-pipeline test: import HAP → Ghidra auto-analysis → decompile all methods → collect metrics → compare baselines.

```bash
# Test a single HAP file
./scripts/ghidra_headless_decompile_test.sh ~/Downloads/entry-default-unsigned.hap

# Test all HAP files in ~/Downloads/
./scripts/ghidra_headless_decompile_test.sh

# Custom timeout (default 30000ms per method)
METHOD_TIMEOUT=60000 ./scripts/ghidra_headless_decompile_test.sh ~/Downloads/large.hap
```

Output goes to `build/ghidra_test_output/`: decompiled `.ts` files, per-HAP logs, and `summary.json` with aggregated metrics. Baseline comparison against `data/test_hap/` detects regression in decompiler output.

Requires: Ghidra extension installed at `~/Documents/ghidra_12.0.4_PUBLIC/Ghidra/Extensions/ark-ghidra/` (run `./gradlew buildExtension` and unzip `build/dist/*.zip` to that location first).

### Building Test HAPs from Source

Use DevEco Studio's toolchain to compile ArkTS source into HAP files for decompiler validation.

**Prerequisites**: DevEco Studio installed at `/Applications/DevEco-Studio.app` (SDK API 24, v6.1.1.115).

**Project location**: `/Users/anakin/DevEcoStudioProjects/MyApplication/`

**Build command**:
```bash
cd /Users/anakin/DevEcoStudioProjects/MyApplication && \
export DEVECO_SDK_HOME=/Applications/DevEco-Studio.app/Contents/sdk && \
/Applications/DevEco-Studio.app/Contents/tools/node/bin/node \
  /Applications/DevEco-Studio.app/Contents/tools/hvigor/bin/hvigorw.js \
  --mode module -p module=entry@default -p product=default assembleHap --no-daemon
```

**Output**: `entry/build/default/outputs/default/entry-default-unsigned.hap`

**Full validation workflow**:
```bash
# 1. Edit source in /Users/anakin/DevEcoStudioProjects/MyApplication/entry/src/main/ets/pages/Index.ets
# 2. Build HAP
# 3. Decompile
./scripts/ghidra_headless_decompile_test.sh /Users/anakin/DevEcoStudioProjects/MyApplication/entry/build/default/outputs/default/entry-default-unsigned.hap
# 4. Compare: data/test_hap/arkts-decompile-test_original_index.ets vs build/ghidra_test_output/arkts-decompile-test.hap__abc_0.ts
```

**ArkTS strict mode restrictions**: No `enum` merging, no constructor property declarations (`private x: string` in constructor params), no nested functions, no `any`/`unknown` types, no `Object.assign` (restricted stdlib), no `for-in` loops, no untyped object literals (`{}` must be typed). Use explicit field declarations in the class body instead.

---

## Claude Loop Workflow

**Notice: Always handle existing improvements before proposing new ones.**

1. **Propose** — Write a plan if there is no existing open issue
2. **Implement** — Write code, tests, and documentation
3. **Verify** — `./gradlew build test lint` must all pass
4. **Commit & Push** — Descriptive message, push to GitHub
5. **Learn** — Capture new conventions into lint rules below
6. **Repeat** — Next most impactful feature

### Rules

- **Never skip tests.** Every feature needs at least one test.
- **Never skip lint.** Fix all lint errors before pushing.
- **One feature per commit.**
- **Push after each feature.** `git push origin main` after green build.
- **Update this file** when new conventions are discovered.

---

## Git Conventions

- **Branch:** Work on `main` (single-developer project)
- **Commit format:** `feat: description` — Prefixes: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `lint:`
- **Push:** After every feature with passing tests

---

## Code Style

- **Language:** Java 21 (Ghidra 12.0.4 requires class version 65.0)
- **Indent:** 4 spaces, no tabs
- **Max line length:** 120 characters
- **Naming:** PascalCase classes, camelCase methods/fields, UPPER_SNAKE constants
- **Imports:** No wildcard imports; order: java. → javax. → ghidra. → com. → project
- **Exceptions:** Use Ghidra's exception hierarchy (ghidra.util.exception)
- **Logging:** Ghidra's Msg class with static OWNER string
- **Null:** No javax.annotation — omit @Nonnull/@Nullable
- **Tests:** JUnit 5, descriptive names: `testParseMethodTable_withThreeEntries_returnsCorrectCount()`
- **No TODO comments** — implement it or create a GitHub issue

---

## Lint Rules

### Checkstyle

- AvoidStarImport, ConstantName, EmptyBlock, LeftCurly(eol), NeedBraces, RightCurly, LineLength(120), Indentation(4), MethodParamPad, ParenPad, TypecastParenPad, WhitespaceAfter, WhitespaceAround, ModifierOrder, RedundantModifier, UnusedImports, UpperEll
- **LeftCurly:** No single-line `{ return x; }`. Multi-line: `{` on same line, body next line, `}` own line.
- **WhitespaceAround:** `{}` must be multi-line (not `{ }`).
- **LineLength:** Must be under `Checker` (not `TreeWalker`) in checkstyle.xml.

### Build

- **JAVA_HOME:** Always set to `/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home` before gradlew.
- **Stale artifacts:** Always `clean` before `test` after code changes. Incremental compilation can produce stale class files.
- **Gradle daemon:** If "NoSuchFileException" for gradle jars, run `--stop` and delete `~/.gradle/daemon/`.

### ArkTS Output Syntax

- Use `let`/`const` (never `var`). No `any` type.
- Access modifiers: `public`/`private`/`protected`. Decorators: `@` prefix.
- Use `struct` for decorated classes, `as` for casting.
- `enum` with optional values, `interface` with optional properties (`prop?: type`).

### Uncertainty Rules

- **Ghidra API:** When unsure about Ghidra plugin interfaces (Loader, Analyzer, Plugin, SLEIGH, etc.), consult the [Ghidra API Javadoc](https://ghidra.re/ghidra_docs/api/) or [Extension Notes](https://ghidra.re/courses/GhidraExternalPluginNotes.pdf) — do not guess API signatures or behavior.
- **ArkTS syntax:** When unsure about ArkTS language syntax or bytecode format, consult the [Ark Bytecode Format](https://gitee.com/openharmony/docs/blob/master/en/application-dev/arkts-utils/arkts-bytecode-format.md) or [OpenHarmony docs](https://gitee.com/openharmony/arkcompiler_ets_frontend) — do not guess syntax rules or opcode semantics.
- **Multi-version compatibility:** The decompiler must handle `.abc` files produced by different ArkTS compiler versions. When adding opcode support or syntax features, verify compatibility across known format variants (e.g., v12 vs v13 ABC header differences). Avoid hardcoding assumptions about a single version's behavior.

### Decompiler Architecture Rules

- **Three entry points:** `decompileInstructions()`, `decompileMethod()`, `decompileFile()`. All three must receive the same post-processing pipeline.
- **Post-processing pipeline order (critical):** `applyConstOptimization → inlineSingleUseVariables → mergeNestedIfConditions → detectSwitchExpressions → simplifyReturnIfTernary → convertIfElseChainToSwitch → removeUnreachableCode → removeAlwaysFalseConditions → simplifyNegatedComparisons → removeUnusedVariables → eliminateDeadPropertyLoads → simplifyIncrementDecrement → eliminateRedundantCopies`. Order matters — each depends on prior transforms. Pipeline is now linear (not nested) for readability and line-length compliance.
- **Pipeline consistency (CRITICAL):** All three entry points (`decompileInstructions()`, `decompileMethod()`, `decompileBody()`) MUST use the same post-processing pipeline. In the past, `decompileMethod()` was missing `removeUnreachableCode` and `removeAlwaysFalseConditions`, causing 207 `if (undefined)` patterns in real output. Later, `decompileInstructions()` was also found missing the same two passes (commit e7d5b97). When adding new passes, update ALL three pipeline locations in `ArkTSDecompiler.java`.
- **StatementResult.NO_OP:** Use for handled instructions producing no statement. Return `null` for unhandled (falls through to `/* opcode */` comment).
- **AST node immutability:** `BlockStatement.body`, `SwitchCase.body`, `VariableDeclaration` use `Collections.unmodifiableList`. Use `setKind()` or rebuild nodes — never `List.set()`.
- **BinaryExpression constructor:** `BinaryExpression(left, operator, right)` — left operand first.
- **ForStatement:** `init`/`condition`/`update` have NO public getters — only `getBody()` is public.
- **Dead store elimination:** Too aggressive — only inline into terminal statements (return/throw) with exactly one usage.
- **Dead property load elimination:** New pass `eliminateDeadPropertyLoads` removes `let ref = obj.method; ref = obj.method(args)` pattern. Uses read-aware counting (`countVariableReadUsage`) to only remove variables never read (only used as assignment targets).
- **Logical NOT compiler pattern:** ArkTS compiler converts `!flag` to `istrue → jnez → ldtrue/ldfalse → return` (not a NOT opcode). The decompiler's block-by-block processing loses the accumulator value at merge points, producing empty method bodies. Needs accumulator propagation across basic blocks (tracked in issue #191 defect 3).
- **Constructor args lost:** `new Pair(1, 2)` decompiles to `new 2()` because accumulator at NEWOBJRANGE time contains a literal (last STA preserves accValue). Needs `resolveCallee` enhancement or pre-instruction accumulator tracking (tracked in issue #191 defect 4).
- **replaceVariable guard:** Never replace variable in LHS of assignment. Leaf expressions must return themselves.
- **When adding new AST types:** Add to `ExpressionVisitor.replaceVariable()`, `countVariableUsage()`, `OperatorHandler.getAccType()`, `InstructionHandler.inferNameFromExpression()`, and const optimization collectors in `ArkTSDecompiler`.

### Testing

- **Parameter naming:** Use `param_0`, `param_1` etc. (not `p0`/`p1`).
- **Test fixtures:** Use 16384-byte buffer, 200-byte spacing between areas. Space sections apart by 50+ bytes.
- **Opcode values in tests:** ALWAYS verify against `ArkOpcodes` constants. Never guess.
- **LiteralExpression in tests:** String values do NOT include surrounding quotes — `toArkTS()` adds them.
- **Robustness tests:** Use try/catch or relaxed assertions (assertNotNull) for edge cases, not specific output assertions.
- **Null safety in tests:** Always null-check `abcFile` parameter since tests pass null.

### Java API Pitfalls

- **Map.of() max 10 entries.** For 11+, use `Map.ofEntries(Map.entry(...))`.

### Agent Coordination

- Agents frequently break builds — always `git checkout HEAD -- .` and verify clean build before agent changes.
- Prefer agents for NEW files only. Reset and re-implement manually if agents break builds.
- Always compile and run full test suite after agent changes.
- Watch for code added outside class closing `}` in test files.

### Error Handling

- **Method-level isolation:** Each method in `DeclarationBuilder.buildClassDeclaration()` and `buildAnnotationClassDeclaration()` is wrapped in its own try-catch. A DisassemblyException in one method emits a comment placeholder and continues with the rest of the class.
- **Disassembler partial results:** `ArkDisassembler.disassemble()` catches DisassemblyException per-instruction and emits a synthetic "truncated" instruction, returning all successfully decoded instructions before the truncation point. Tests must account for this — FuzzingTest now checks for `"truncated"` mnemonic instead of expecting exceptions.

### 16-bit Variant Opcodes (isa.yaml multi-encoding)

Many Ark instructions have **multiple `opcode_idx` values** — an 8-bit IC slot variant and a 16-bit IC slot variant. Both are PRIMARY opcodes (not 0xFD-prefixed). The 16-bit variants use wider IC slot fields but have the same operand count and order. The decompiler normalizes them via `ArkOpcodesCompat.normalizeVariantOpcode()` so `InstructionHandler` treats them identically to their 8-bit counterparts. When adding new instruction support, check `isa.yaml` for all `opcode_idx` values — there may be 2-3 variants per instruction (e.g., `jmp` has 0x4D/0x4E/0x98 for 8/16/32-bit offsets, `mov` has 0x44/0x45/0x8F for 4/8/16-bit registers).

**Authoritative source:** `https://gitee.com/openharmony/arkcompiler_runtime_core/raw/master/isa/isa.yaml` — contains all opcode_idx values, formats, and properties. Always consult this when adding opcode support.
- **Class-level catch still exists** in `ArkTSDecompiler.decompileFile()` as a safety net for non-method errors (field parsing, decorator detection, etc.).

### E2E Test Pipeline

- **Ghidra extension must be rebuilt and reinstalled** before running headless E2E tests. The headless script uses the installed extension JAR, not the local build. Without reinstalling, old code produces false errors.
- **ABC_BLOCK_PADDING:** HAP loader uses dynamic spacing based on actual block sizes, with 1MB alignment. Previous fixed 1MB spacing caused memory conflicts for large ABC blocks (>1MB).
- **Encoding:** Use `result.getBytes(StandardCharsets.UTF_8)` + `Files.write()` instead of `Files.writeString()` to handle unmappable characters in decompiled output.
- **Shell compatibility:** Use `while IFS= read -r` instead of bash-only `mapfile` in test scripts.

### Output Quality

- **sanitizeClassName:** Strips `L...;` wrapper, `&version` suffix, and extracts short name from ABC class names (e.g., `L&@pkg/Index&1.0.3;` → `Index`).
- **sanitizePropertyName:** Replaces control characters with `_XX` hex escapes in property names.
- **sanitizeMethodName:** Decodes ABC-encoded method names (`#~@N>#name` → `name`, `#~@N=#name` → constructor detection, `#*#name` → static method). Falls back to `anonymous_method` for empty/invalid names.
- **isConstructorMethod:** Detects constructors via `<init>`, `<ctor>`, class name match, and `#~@N=#name` ABC encoding pattern.
- **MemberExpression:** Falls back to bracket notation for non-identifier property names.
- **Metadata field filtering:** Internal ABC fields (pkgName, isCommonjs, hasTopLevelAwait, isSharedModule, scopeNames, moduleRecordIdx) are excluded from output.
- **definepropertybyname:** Simplified to direct property assignment (`obj.prop = value`) instead of verbose `Object.defineProperty(...)`.
- **Variant opcode normalization:** `InstructionHandler.processInstruction()` now normalizes 16-bit variant primary opcodes (MOV_8, MOV_16, etc.) via `ArkOpcodesCompat.normalizeVariantOpcode()`. Previously only 0xFD wide sub-opcodes were normalized, causing 5462 mov instructions to appear as comments.
- **Property access operand mapping (CRITICAL):** Ark bytecode property access opcodes have non-intuitive operand-to-semantics mapping:
  - `LDOBJBYVALUE` (IMM8_V8): acc=key, V8=object → `object[key]`
  - `LDOBJBYINDEX` (IMM8_IMM16): acc=object, IMM16=index → `object[index]`
  - `STOBJBYVALUE` (IMM8_V8_V8): acc=value, V8₁=object, V8₂=key → `object[key] = value`
  - `STOBJBYINDEX` (IMM8_IMM16_V8): acc=object, IMM16=index, V8=value → `object[index] = value`
  - `STOBJBYNAME` (IMM8_IMM16_V8): acc=value, IMM16=name, V8=object → `object.name = acc`
  - `STTHISBYNAME` (IMM8_IMM16_V8): acc=value, IMM16=name → `this.name = acc`
  - The acc role differs between BYVALUE (acc=key), BYINDEX (acc=object), and BYNAME (acc=value) variants.

### Decompiler Quality Audit (entry-default-unsigned.hap, 5595 methods)

- **0 unhandled opcode comments** (all mov/ldlexvar/stlexvar now handled)
- **16 unknown_ opcode references** remaining
- **1103 trivial methods** (just parameter copying, ~20%)
- **~2257 unreachable code blocks after throw** — FIXED: `removeUnreachableCode` post-processing
- **~928 void methods with return <value>** — PARTIALLY FIXED: `filterVoidMethodReturns` strips `return undefined;`
- **~198 `if (undefined)` always-false conditions** — FIXED: `removeAlwaysFalseConditions` post-processing
- **417 acc references outside comments** — raw accumulator leaking into output
- **Method name decoding** — FIXED: `sanitizeMethodName()` decodes `#~@N>#name` patterns
- **Constructor detection** — FIXED: `isConstructorMethod()` detects `#~@N=#name` patterns
- **Throw prefix handling** — FIXED: Only `throw` (0xFE 0x00) emits ThrowStatement; runtime assertion throws (0xFE 0x01-0x09) now produce NO_OP
- **Async method abstract detection** — FIXED: Methods with ACC_ABSTRACT flag but non-empty bodies are no longer rendered as abstract
- **Getter/setter ABC encoding** — FIXED: `isAccessorPrefix()` detects `#~@N<#name` pattern for getter/setter identification
- **INSTANCEOF opcode** — VERIFIED: Already fully handled in OperatorHandler (binary operator "instanceof")
- **STARRAYSPREAD opcode** — VERIFIED: Already handled in LoadStoreHandler with SpreadExpression AST node
- **Remaining:** import resolution (`import_N` placeholders), acc leaking, string resolution (str_N for unresolved LNP strings)

### Vendor-Specific Named Call Opcodes (0xDD-0xE1)

HarmonyOS API 12+ compilers emit opcodes beyond standard isa.yaml `last_opcode_idx: 0xDC`. These are **named call variants** that embed the method name (ID16 string index) directly in the instruction encoding. Confirmed via official `ark_disasm` tool from HarmonyOS SDK (`/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/ark_disasm`).

| Opcode | Mnemonic | Format | Length | Description |
|--------|----------|--------|--------|-------------|
| 0xDD | callthis0withname | IMM8_IMM16_V8 | 5 | Call this.method() with 0 args, name in instruction |
| 0xDE | callthis1withname | IMM8_IMM16_V8_V8 | 6 | Call this.method(arg) with 1 arg, name in instruction |
| 0xDF | callthis2withname | IMM8_IMM16_V8_V8_V8 | 7 | Call this.method(a,b) with 2 args, name in instruction |
| 0xE0 | callthis3withname | IMM8_IMM16_V8_V8_V8_V8 | 8 | Call this.method(a,b,c) with 3 args, name in instruction |
| 0xE1 | callthisrangewithname | IMM8_IMM8_IMM16_V8 | 6 | Call this.method(args...) with range, name in instruction |

These complement the standard callthis0-callthis3 (0x2D-0x30) and callthisrange (0x31) which do NOT embed the method name. The named variants resolve the method name from the string table (operands[1] or operands[2] for range), allowing the decompiler to produce `obj.methodName(args)` directly. The `normalizeVariantOpcode()` in `ArkOpcodesCompat` maps them to their base equivalents so existing call handling logic works unchanged.

### Loop Iteration Notes

- **Open issues as of 2026-05-12:** #184, #186, #200, #201, #209, #210 (6 total). Issues #185, #196 closed.
- **UI improvements this iteration (2026-05-12):** [Ann] annotation badge + Ann filter; method outline dropdown in ArkTS Output header (syncs to caret); single-click method in HAP Explorer decompiles immediately; jump-to-definition resolves method names; decompileWholeFile runs in background SwingWorker; File View toggle groups classes by source file with directory hierarchy; Decompile All Visible button; hover tooltip shows decompiled method preview (JEB-like peek).
- **UI improvements continued (2026-05-12):** Panel title shows current method/class; Export Visible in HAP Explorer context menu; N/X/Ctrl+Click JEB shortcuts (N=rename, X=xref, Ctrl+Click=jump to def); method outline shows return type; Decompile All Visible label shows filter type; Ctrl+Shift+O focuses method outline; method outline shows size indicators (●/○); hover over class name shows class summary tooltip.
- **UI improvements (2026-05-12 batch 3):** Show in HAP Explorer button (bidirectional navigation); color-coded method outline (red=large, orange=medium); progress bar in status bar (N/M); decompile all abilities publishes N/M progress; Ctrl+D decompiles current class; right-click Copy Method Signature.
- **UI improvements (2026-05-12 batch 4):** Quick Open shows method count + [recent] tag + Ctrl+Enter decompiles class; color-coded line number gutter (red=large, orange=medium, green=small); Xref panel "Search All Methods" button; Call Graph single-click navigation; Bookmarks + Global Search single-click navigation.
- **UI improvements (2026-05-12 batch 5):** Stats panel shows class type breakdown + top 5 largest methods + complexity distribution; HapInfo single-click navigation; Notes shows note preview tooltip; clicking method name auto-updates Call Graph; method filter field in header (type to filter outline).
- **UI improvements (2026-05-12 batch 6):** Global Search shows context tooltip (2 lines before/after match); HAP Explorer filter supports /regex/ syntax.
- **Closed this iteration:** #211 (inline string literals — VariableDeclaration target inlining), #207 (comparison operators — defect 1 fixed, defects 2-3 are accumulator state loss), #185 (quality audit — 4/6 defects resolved).
- **Fixed this iteration (continued):** #200 defect 1 (push call lost — single-successor JUMP blocks now preserved via liveContinuations in isDeadCode check).
- **Constructor callee lastClassCandidate (commit 586fead):** `setLastClassCandidate` now called when LDA loads a register with class-like expression and when TRYLDGLOBALBYNAME loads a capitalized global name. Previously `setLastClassCandidate` was defined but never called, so the fallback in `handleNewObjRange` never triggered.
- **CALLTHIS register scan improvement (commit 586fead):** `recoverCalleeFromRegisters` now has a second pass that finds any non-literal VariableExpression (not just MemberExpression) for function reference recovery. Helps when method reference was stored as a plain variable rather than property access.
- **VariableDeclaration inline target (commit 1a12f81):** `inlineSingleUseVariables` now handles VariableDeclaration as inline target via `tryInlineInto`. Pattern: `const v0 = "tag"; let v1 = v0` → `let v1 = "tag"`.
- **DefineFuncExpression lex var name inference (commit 92279a8):** `inferLexVarName` now handles `DefineFuncExpression` — when a function is stored to a lexical variable via STLEXVAR, the variable name is inferred from the resolved method name. Helps closure patterns show function names instead of `lex_0_N`.
- **ConditionalExpression null-safety (commit 72c5c24):** `ConditionalExpression.toArkTS()` now handles null `consequent` or `alternate` by emitting "undefined". Prevents NPE in ternary expression rendering.
- **Remaining Kazumi `new acc()` (80):** The constructor callee issue persists because the class reference (loaded via TRYLDGLOBALBYNAME) is stored in a register, but that register is later overwritten by argument loading. The `firstArgExpr` check catches the case where the class IS the first argument, but for `new Map()` (0 args) the register tracking may lose the class reference across basic block boundaries. Needs cross-block register preservation.
- **Fixed this loop:** #207 defect 1 (comparison operators inverted — commit 29aea6e), #197 defect 1 (arithmetic precedence), #197 defect 3 (if-else-if operators correct), #202 defect 1 (empty loop bodies verified fixed), acc-value ternary detection (commit a5aff78), #208 while loop condition inversion (commit 4eadbfd).
- **Fixed this loop (continued):** DEFINECLASSWITHBUFFER format fix (commit 5aa99ca), negated comparison simplification (commit b9b63c9), boolean constant folding (commit 6201bc2), if(true) simplification (commit 3dbb6fa), cross-statement single-use variable inlining (commit efaf73f), acc. leakage fix for CALLTHIS (commit a467ebf), try-catch body control flow reconstruction (commit 005c8b1).
- **Root cause of operand order bug:** The isa.yaml pseudo `acc = ecma_op(acc, operand_0)` is misleading. The Ark compiler loads the LEFT source operand into a register and the RIGHT into acc. The VM computes `register OP acc`. Previous commit 3b9e2ea incorrectly "fixed" the order based on isa.yaml, causing `>=` to invert to `<=`. Reverted in commit 29aea6e.
- **E2E stats:** 27,389+ methods across 6 HAP files (real-world), ~1700 methods on known-source test HAPs (Rounds 1-50) — 0 failures, 0 timeouts, 0 unhandled opcodes across all. Melotopia 11,788 methods, Kazumi 3,632 methods — both fully decompiled.
- **Quality metrics (Kazumi, 3632 methods):** if(undefined)=0, while=78, for=14, do-while=65, if=1247, switch=4, try=456, lex_=3621, unknown_=29, new acc()=71, acc.=844, if(!)=15 (was 452, reduced 97% by negated comparison simplification + if(true) folding). `func_` count of 351 is a false positive — all are `func_main_0` (correct module init function name). Acc-value ternary: 4 `return cond ? a : b` patterns detected.
- **Quality metrics (Melotopia, 11788 methods):** new acc()=188 (reduced via register scan), if=1557, while=49, for=15, &&=5, ||=13, lex_=8071.
- **Known-source test HAPs (62 rounds):** Built from `/Users/anakin/DevEcoStudioProjects/MyApplication`. Each round tests specific language patterns. Baselines in `data/test_hap/arkts-decompile-test{1-62}_*.{ets,ts}`.
- **Patterns tested across rounds:** Math (basic + extended: pow, hypot, sign, trunc, log2, cbrt, sqrt, cos, sin, min, max, abs, ceil, floor, round, PI), bitwise (AND, OR, XOR, NOT, shifts << >> >>>, masks), string methods (charAt, charCodeAt, substring, startsWith, endsWith, replace, replaceAll, split with limit, join, indexOf, includes, slice, padStart, padEnd, trim, trimStart, trimEnd, repeat, search, match, codePointAt, fromCharCode, localeCompare), array methods (push, pop, shift, splice, slice, reverse, sort with comparator, every, some, map, filter, find, findIndex, reduce, concat, includes, indexOf, join, from, fill, copyWithin), class/inheritance (multi-level 3-deep), constructor, getters/setters, static methods/factory, fluent API, interface implementation, custom Error, closures, higher-order functions (composition, currying, function returns), recursion, linked list, stack, queue, binary tree, observer pattern, state machine, builder pattern, try-catch, Map/Set, Promise, generics, typeof guards, instanceof, Array.isArray, enum-const (HttpStatus), RegExp (exec/test), ArrayBuffer/DataView (setUint8/getUint8), TypedArrays, WeakRef, optional chaining, nullish coalescing, template literals, escape strings, for/while/do-while/for-of loops, break/continue, labeled continue, switch/default, if-else-if, ternary (including nested), boolean logic (&&, ||, !), compound assignment (+=, -=, *=, /=, %=), modulo all sign combos, deep equality (===, !==), string comparison, increment/decrement patterns, number toFixed, Number constants (isInteger/isNaN/isFinite), polymorphism via interface, super calls, Math.sqrt, nested property chains, function refs via lex variables, distance formula, GCD, coprime check, binary search, bubble sort, insertion sort, merge sort, Caesar cipher, prime check, fibonacci (recursive + iterative), FizzBuzz, palindrome check, fast exponentiation, matrix operations, date methods, JSON.parse/stringify, Object.keys/values, nested Records, unary operators, default parameters, rest parameters, nested try-catch with throw, Object.assign (restricted in ArkTS), Map advanced (has/delete/get/size/forEach, nested Maps), Set operations (add/has/delete/size, intersection/difference), typeof type guard with narrowing, instanceof with custom class hierarchy, class-based enum (HttpStatus), String.fromCharCode range, Number.toString radix, bitwise flag patterns, String.concat, Validator chain pattern, StringBuilder chain, power-of-two check, leap year check, reverse words, chained range comparison, multi-dimensional array creation, word frequency counting, BST (insert/search/inorder traversal), LRU cache (doubly-linked list + Map), sliding window max sum, two-pointer deduplication, merge sort (divide and conquer), Dijkstra shortest path, tail recursion, mutual recursion (isEven/isOdd), binary tree path sum, string compression, quick sort (in-place partition), graph BFS, Trie (insert/search/startsWith), producer-consumer buffer, retry with exponential backoff, HttpStatus enum class, manual optional chaining (null guard chains), nullish coalescing chains, array destructuring manual, object merge manual, SimplePromise chain (fluent API), nested closures (counter factory), function composition (compose(f, g)), higher-order filter/map/reduce chain, memoization with Map cache, currying.
- **Priority:** Fix decompiler quality issues. Vendor opcodes DD-E1 now fully handled. Key remaining: acc. method leakage (844 sites — top: acc.set=346, acc.success=77, acc.push=23), constructor callee recovery (71 `new acc()` on Kazumi), lex_ variable names (3621 — ABC format limitation, 88% at level 0), try-catch body reconstruction (456 try blocks, 266 with empty bodies).
- **DEFINECLASSWITHBUFFER format fix (commit 5aa99ca):** 8-bit variant (0x35) was mapped to IMM8_IMM16_IMM16_V8 (7 bytes) but isa.yaml defines it as IMM8_IMM16_IMM16_IMM16_V8 (9 bytes). 16-bit variant (0x75) was wrongly named WIDE_SUPERCALLTHISRANGE, should be DEFINECLASSWITHBUFFER_16 with IMM16_IMM16_IMM16_IMM16_V8 (10 bytes). 2-byte shortfall caused cascading misalignment.
- **Negated comparison simplification (commit b9b63c9):** Post-processing pass `simplifyNegatedComparisons` converts `!(a === b)` → `a !== b`, `!(a < b)` → `a >= b`, etc. Reduced `if (!)` patterns from 452 to 20 on Kazumi.
- **Acc-value ternary detection (commit a5aff78):** `detectTernaryPattern` now detects ternary expressions where both branches load a value into the accumulator (no STA) instead of assigning to a variable. When merge block is just `return`, emits `return cond ? val1 : val2`. Currently only detects simple two-branch ternaries; nested ternary chains (a ? b : c ? d : e) still produce if-else chains because the continuation block fails `isAccValueBlock`. Future: post-process if-else-return chains to ternary.
- **Unknown opcode investigation:** All 30 unknown_ references in Kazumi output are instruction alignment issues, NOT real opcodes. Evidence: `unknown_75` appears (0x75 is a valid opcode = DEFINECLASSWITHBUFFER_16), ark_disasm produces zero unknowns from same ABC. Bytes 0xE2-0xFA, 0xFF are data bytes read as opcodes due to SLEIGH module consuming wrong number of bytes for some instruction. Deprecated prefix (0xFC) not handled as prefix may contribute.
- **While/for loop condition inversion fix (commit 4eadbfd, issue #208):** The negation logic in `processWhileLoop`/`processForLoop` used `isBranchOnFalse(opcode)` to decide whether to negate the condition. But the CFG always maps CONDITIONAL_TRUE=jump-target and CONDITIONAL_FALSE=fall-through regardless of opcode semantics. Fixed by tracking `loopBodyIsJumpTarget` in the pattern and using `isBranchOnFalse(opcode) XOR loopBodyIsJumpTarget` for the correct negation decision. Previously `while (i < arr.length)` was decompiled as `while (i >= arr.length)`.
- **Cross-statement variable inlining (commit efaf73f, issue #211):** `inlineSingleUseVariables` now scans ALL subsequent statements (not just the immediately next one) and inlines when a variable is used exactly once. This reduces intermediate variable noise like `const v7 = "testTag"; ...; call(v7)` → `call("testTag")`. Added `countVariableUsageInStmt` for statement-level variable counting.
- **Acc. leakage analysis (Kazumi, 844 sites):** Top patterns: acc.set (346), acc.success (77), acc.messageHandler (35), acc.add (29), acc.push (23). Root cause: accumulator value (`currentAccValue`) lost across basic block boundaries and when overwritten by literal loads. 60-70% could be fixed by checking previous instruction's result before defaulting to `acc.`.
- **Acc. leakage fix (commit a467ebf):** `PropertyAccessHandler.translateCall` now recovers callee from registers for CALLTHIS when accValue is null/unresolved (not just for literals). Added `wrapCallThisCallee` to avoid double-wrapping MemberExpressions when callee is already a recovered MemberExpression.
- **Lex_ variable analysis (Kazumi, 3621 sites):** 88% at level 0 (3190), 10% at level 1 (377), 2% at level 2+ (61). ABC format does NOT store lexical variable names — the decompiler can only infer names from stored expressions via `setInferredName`. This is a fundamental format limitation.
- **Try-catch body fix (commit 005c8b1, issue #203):** `processTryCatch` now uses `reconstructSubGraph` for try body blocks instead of flat `processBlockInstructions`. This enables proper if/else/loop detection inside try blocks. Nested try-catch regions still handled directly. Previously 266/456 try blocks had empty bodies.
- **Try-catch body analysis (Kazumi, 456 try blocks):** 266 have empty bodies (`try { }`). Pattern: `try { } catch (e) { actual_code }` — code that should be in the try body appears in the catch handler instead. Root cause was flat `processBlockInstructions` missing control flow inside try range.
- **New issues:** #209 (page/ability-level output granularity), #210 (undefined register variables v6/v7/v8).
- **Do-while detection fix (commit 1e41e08):** When the back-edge target (loop body) starts at a lower offset than the condition block, the loop is a do-while (body executes before condition). Previously all such loops were misclassified as while loops. Added check in `ControlFlowReconstructor.detectPattern()` before defaulting to WHILE_LOOP.
- **isDeadCode fall-through fix (commit 5604bda):** `isDeadCode` in `ControlFlowReconstructor` now checks if any visited predecessor has FALL_THROUGH or CONDITIONAL_FALSE edge to the block. These edge types indicate the block is a natural continuation, not dead code. Previously, blocks reachable only from visited predecessors via fall-through were incorrectly marked as dead, causing empty method bodies after if-else and guard clauses.
- **isDeadCode single-successor JUMP fix (commit f5198b3):** Blocks reachable only via a JUMP edge from a visited predecessor were incorrectly marked as dead. Now, blocks that are the sole successor of another block (single outgoing JUMP edge) are added to `liveContinuations` during dead code check initialization. This handles compiler-inserted jumps between sequential blocks (e.g., for try-catch regions or alignment). Fixes CALLTHIS instructions in separate basic blocks being lost (e.g., `this.items.push(val)` producing empty method body). Note: reveals ~20 previously-hidden `acc.set` calls on Kazumi.
- **CALLTHIS callee recovery (commit 6fc5c4f):** When CALLTHIS opcodes have acc holding a literal (last stored argument), the method reference was lost. `PropertyAccessHandler` now scans tracked register expressions for MemberExpression to recover the callee. Reduces `acc.method()` patterns on Melotopia (188 `new acc()` remaining on Kazumi due to cross-block accumulator loss).
- **if-else-if chain truncation (issue #197 defect 3):** Unit tests confirm basic if-else-if chains work correctly for both jeqz and jnez encodings. Test10's specific issue is likely caused by the real compiler's `istrue` pattern (accumulator state lost after boolean conversion), not by the pattern detection itself. Needs actual bytecode to diagnose further.
- **Loop body reconstruction (commit 6f08323):** `processWhileLoop`, `processDoWhile`, `processForOfLoop`, `processForInLoop`, `processForAwaitOfLoop` now use `collectLoopBodyBlocks` to gather ALL blocks in the loop range, then `reconstructSubGraph` processes them recursively. Previously only the first body block was processed, causing empty loop bodies when the loop contained conditionals.
- **Multi-block if/else branches (commit e6909b7):** `processIfElse` and `processIfOnly` now use `collectIfBranchBlocks` to gather all blocks in each branch sub-graph, then `reconstructSubGraph` processes them recursively. `reconstructSubGraph` skips dead code checking for explicitly-selected blocks, preventing false dead code elimination when predecessor blocks are already visited by the parent call.
- **Accumulator propagation (commit d121ae3):** `BlockInstructionProcessor` now reads `ctx.currentAccValue` instead of starting each block with null. `BranchProcessor.processIfElse` saves/restores accumulator around branches. This propagates register state across block boundaries, fixing conditions and expressions that were lost at merge points.
- **Zero-arg constructor recovery:** `handleNewObjRange` extends literal recovery to zero-arg case: when acc holds a literal and numArgs == 0, checks the first register for a class reference. Eliminates all `new 0()` patterns.
- **Else-if chain reconstruction (commit b066ad5):** `processIfElse` recursively processes else-blocks that contain conditional branches via `reconstructControlFlow` instead of `processBlockInstructions`. Uses `collectBlocksBetween` for sub-CFG extraction. Prevents if-else-if chains from being truncated after the first branch.
- **Fall-through continuation fix (commit c154052):** Added `liveContinuations` tracking to prevent `isDeadCode` from skipping fall-through blocks after IF_ONLY patterns. Previously, blocks reachable only from a visited predecessor were incorrectly treated as dead.
- **DEFINEFUNC/DEFINEMETHOD name resolution (commit e8278fe):** `resolveMethodName()` looks up actual function/method names from ABC file's method table instead of emitting `func_N`/`method_N`. Reduces func_ references by 89% (3348 → 368).
- **Lexical variable name inference (commit 5791c89):** `STLEXVAR` infers variable names from stored expressions (function names, property names, method names). `LDLEXVAR` uses inferred names when available. Reduces lex_0 references by ~4% (4648 → 4471).
- **Open issues consolidated:** #196 (do-while, recursive self-call, lex vars, chained &&), #197 (arithmetic precedence, boolean logic, if-else-if), #198 (null guard, getter detection, key-value swap), #200 (push lost, Map.has, lex vars), #201 (while-loop+new, sort comparators, items[0] corruption), #202 (algorithm loops empty, charAt reuse, unary ops), #203 (try-catch-finally). Issues #189-#195 closed as consolidated into these.
- **Binary operand order:** The Ark compiler loads the LEFT source operand into a register and the RIGHT operand into acc. The VM computes `register OP acc`. `InstructionHandler` constructs `BinaryExpression(reg, op, acc)` = `left OP right`. Verified via ark_disasm: for `a.length - b.length`, acc=b.length, reg=a.length; for `s >= 90`, acc=90, reg=s. The isa.yaml pseudo `acc = ecma_op(acc, operand_0)` is misleading — operand_0 is the register (left operand), not the right operand.
- **Constructor callee recovery:** Multi-strategy approach in `handleNewObjRange`: (1) check firstReg for class-like expression, (2) scan all tracked registers (highest first) for class-like expression, (3) use `lastClassCandidate` from `DecompilationContext` (set when LDA loads a class-like expression). Still 629 `new acc()` on Kazumi due to TRYLDGLOBALBYNAME class loading and cross-block acc loss — needs deeper investigation.
- **STOBJBYVALUE operand roles (commit eec42bb):** acc=value (not key), operands[2]=key (not value). The compiler loads the key into a register first, then the value into acc.
- **Getter/setter detection safety (commit 1f9dfb2):** `isGetterMethod`/`isSetterMethod` now require successful proto resolution before classifying as accessor. When proto is null, paramCount was defaulting to 0, causing non-getter methods with `#~@<` prefix to be rendered as getters.
- **Getter detection numArgs cross-check (commit 7a4dfa2):** ABC proto shorty can be incorrect for some methods (size 1 = return only, no params). Added cross-check against `code.getNumArgs()`: methods with non-zero numArgs are NOT getters, preventing static factory methods from being misclassified as `static get` accessors.
- **Else-if chain duplicate merge block (commit 0a7f2e9):** In `processIfElse`, nested if-else (else-if chains) share the same merge block with the parent. The inner if-else must NOT process the merge block — only the outermost owner should. Without this guard, extra merge-block statements pollute the else sub-graph, preventing `unwrapSingleIf` from flattening `else { if (...) { } }` into `else if (...) { }`.
- **Inferred variable name uniquification (commit 9ae6709):** `DecompilationContext.setInferredName()` now uniquifies names by appending numeric suffix (e.g., charAt2, charAt3) when the same inferred name is already used by another register. Prevents self-comparison bugs like `charAt !== charAt`.
- **Bitwise NOT fix (commit 65a2947):** NOT opcode (0x20) is shared between `~` (bitwise) and `!` (logical) in Ark bytecode. `OperatorHandler.getNotOperator()` checks operand type: number → `~`, else → `!`. `TypeInference` updated to return `number` for NOT when operand is numeric.
- **NOT format confirmed:** isa.yaml confirms NOT has `op_imm_8` format (JIT IC slot), NOT `op_none`. ArkOpcodes.java mapping is correct. Test bytecode must include IC slot byte after NOT opcode.
- **Dead store elimination deferred:** The pattern `let ref = obj.method; ref = obj.method(args)` needs dead store elimination, but naive read-only usage tracking breaks test cases where variables are reassigned without being read. Needs more careful implementation — only remove when variable is truly unused (not even as assignment target) OR strip initializer but keep declaration.
- **Guard clause dead code fix (commit a0203d8):** `tryProcessGuardClause` in `BranchProcessor` now marks the `otherBlock` as a `liveContinuation` when both branches end with return. Previously, the fall-through block was skipped by `isDeadCode` because its predecessor (the condition block) was already visited, causing the entire continuation path to be lost — producing empty method bodies.
- **Binary operand order FIX (commit 29aea6e, issue #207 defect 1):** Binary ops operand order corrected: `register` is LEFT, `acc` is RIGHT. Previous commit 3b9e2ea incorrectly changed from `BinaryExpression(reg, op, acc)` to `BinaryExpression(acc, op, reg)` based on isa.yaml pseudo code. Real VM behavior (confirmed via ark_disasm on real HAP bytecode) shows the compiler loads left operand into register and right operand into acc. The 3b9e2ea "fix" was a regression that caused `>=` comparisons to be inverted to `<=` in if-else-if chains.
- **Agent coordination lesson:** Background agents can conflict with main thread edits. Prefer manual edits for files shared across features. Always `git checkout HEAD -- .` and verify clean build before committing.
- **Chained AND via JNEZ (commit 33d7690, issue #196 defect 4):** When the Ark compiler uses JNEZ (jump-if-true) for `&&` conditions, the next condition block is in the trueBranch (CONDITIONAL_TRUE edge). The fall-through is the merge/else block. Both blocks sharing the same fall-through offset indicates a chained AND using JNEZ encoding. Added detection in `BranchProcessor.detectShortCircuitPattern`.
- **Register scan for newobjrange callee (commit 470aa9e):** When accValue is a literal at NEWOBJRANGE, scans all tracked registers (highest first, excluding argument range) for a class-like expression (VariableExpression or MemberExpression). Also added `lastClassCandidate` tracking in `DecompilationContext` — set when LDA loads a register with a class-like stored expression. Helps for Melotopia (188 `new acc()` out of 11788 methods) but not Kazumi (629 still failing due to TRYLDGLOBALBYNAME class loading).
- **Wide opcode format:** Wide sub-opcodes are in the 0x80+ range. Sub-opcodes below 0x80 (like 0x28) produce `wide_unknown_XX` mnemonic with UNKNOWN format (length=1) — they don't throw. Test only recognized wide opcodes when testing truncation behavior.
- **typeof type guard (issue #205):** Functions using `typeof value === 'number'` decompile to completely empty bodies. The istrue→jnez pattern for typeof checks loses accumulator state at merge points (similar to #191 defect 3).
- **Reserved keyword variable names (issue #206):** FIXED (commit 877da37). `DecompilationContext.sanitizeKeyword()` prefixes JS/ArkTS reserved keywords with `_` when used as variable names. Applied in both `setInferredName` (inferred names) and `resolveRegisterName` (debug names).
- **Default parameter handling:** Ark compiler encodes default parameters as `undefined` check at function entry. The decompiler partially detects this but variable tracking breaks — return values are garbled.
- **Rest parameter handling:** `...nums` compiles to array creation from arguments. The loop iterating rest array hits issue #202 (empty loop body) since it's a multi-block loop.
- **void operator:** Correctly resolves to `undefined` in decompilation.
- **super calls in multi-level inheritance:** `super.double()` is visible but return value tracking breaks — `double2` references are undefined.
- **try-finally:** `try { ... } finally { ... }` (no catch) decompiles to `throw undefined`, losing both try and finally blocks entirely. try-catch-finally partially preserves catch but drops finally block. Both tracked in #203.
- **If-else-if chain:** 4-level if-else-if chain structure is FULLY preserved with all conditions and labels. Returns wrong branch due to variable re-declaration confusion but structure is correct.
- **ArkTS restrictions (Round 31 confirmed):** `void expr` result type is `undefined` (not `number`), must use `number | undefined`. Array index out of bounds returns `undefined`, not assignable to `number`.
- **Throw prefix sub-opcodes (0xFE prefix):** Only 0x00 is a real throw. Sub-opcodes 0x01-0x09 (throw.notexists, throw.patternnoncoercible, throw.deletesuperproperty, throw.constassignment, throw.ifnotobject, throw.undefinedifhole, throw.ifsupernotcorrectcall, throw.undefinedifholewithname) are compiler-inserted runtime checks — they produce NO_OP, not ThrowStatement.
- **ABC string resolution:** `resolveStringByIndex()` uses the combined method/string/literal index (`MethodStringLiteralRegionIndex`) from the region header, NOT the LNP index. The region header's `method_idx_off`/`method_idx_size` fields point to a combined array of offsets for methods, string constants, and literal arrays. LDA_STR's `string_id` indexes into this combined array. Each entry points to a `String` structure (uleb128 `utf16_length` + null-terminated MUTF-8 data). Non-string entries (methods, literal arrays) will fail `readMutf8()` and are left as null in the cache. This is confirmed across ABC v24.0.0.0 and v13.0.1.0 files.

---

## Build Dependencies

- Ghidra 12.0.4 (extension API)
- Java 21 (required)
- Gradle 8.x (wrapper included)
- JUnit 5 (testing)
- SpotBugs + Checkstyle (static analysis)

---

## Key References

- [Ghidra Extension Notes](https://ghidra.re/courses/GhidraExternalPluginNotes.pdf)
- [Ghidra API Javadoc](https://ghidra.re/ghidra_docs/api/)
- [Ark Bytecode Format](https://gitee.com/openharmony/docs/blob/master/en/application-dev/arkts-utils/arkts-bytecode-format.md)
- [OpenHarmony Ark Compiler](https://gitee.com/openharmony/arkcompiler_ets_frontend)
