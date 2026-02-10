# OPA-to-Drools Rule Converter

A browser-based tool that converts Oracle Policy Automation (OPA) rule documents (`.docx`) into Drools Rule Language (`.drl`) files and companion Java fact classes for use with a Spring Boot Drools engine.

Built with Python and Flask. Runs locally in your browser.

---

## Prerequisites

- Python 3.8+
- pip

## Installation

```bash
pip install flask python-docx
```

## Running the App

```bash
python opa_to_drools_converter.py
```

Open **http://localhost:5000** in your browser.

---

## How It Works

The conversion pipeline has three stages:

### 1. Parse — Read the OPA Document

The parser opens each `.docx` file and classifies every paragraph by its Word style:

| OPA Word Style | Role |
|---|---|
| `OPM - conclusion` | The rule's conclusion (the "then" outcome) |
| `OPM - level 1` | Top-level conditions that prove the conclusion |
| `OPM - level 2` | Sub-conditions that prove a level 1 condition |
| `OPM - level 3` / `level 4` | Deeper sub-conditions |
| `OPM - rule name` | Section/document name |

If no OPA styles are detected (e.g. the document was reformatted), the parser falls back to **indentation-based parsing** and **keyword detection** (`if`, `when`, `must`, `required`, etc.).

Conditions connected by `and` or `or` lines are recognized and preserved as the logical operator for that rule.

### 2. Convert — Map OPA Phrases to Drools Constraints

Each natural-language condition is matched against a table of regex patterns and translated to a Drools constraint expression:

| OPA Phrase | Drools Output |
|---|---|
| `the X is not null` / `is known` / `is provided` / `is present` / `has a value` / `exists` / `is not empty` | `x != null, x != ""` |
| `the X is null` / `is unknown` / `is missing` / `is empty` | `x == null \|\| x == ""` |
| `the X is greater than 100` / `more than` / `above` / `exceeds` | `x > 100` |
| `the X is less than 50` / `below` / `under` | `x < 50` |
| `the X is greater than or equal to 18` / `at least` | `x >= 18` |
| `the X is less than or equal to 10` / `at most` / `no more than` | `x <= 10` |
| `the X equals "Active"` / `is equal to` / `is` | `x == "Active"` |
| `the X is not equal to 0` / `does not equal` | `x != 0` |
| `the X is true` / `is false` | `x == true` / `x == false` |
| `the X contains "abc"` | `x contains "abc"` |
| `the X is between 10 and 20` | `x >= 10 && x <= 20` |
| `the length of X is greater than 5` | `x.length() > 5` |

**Empty string handling:** All presence checks (`is not null`, `is known`, `is provided`, etc.) generate **both** a null check and an empty-string check (`!= null, != ""`). This ensures that `""` is treated as invalid, matching OPA's behavior where "known" means meaningfully present.

**OR conditions:** When a rule uses `or` between conditions, each multi-part check is grouped in parentheses:
```
(fieldA != null && fieldA != "") ||
(fieldB != null && fieldB != "")
```

Conditions that can't be auto-matched get a `/* TODO: Review */` comment and are flagged in the UI.

### 3. Generate — Produce DRL + Java Files

For each uploaded document the converter outputs:

- **A `.drl` file** — One Drools rule per OPA conclusion, using MVEL dialect, with `when`/`then` blocks, comments referencing the source, and conversion timestamps.
- **A `.java` fact class** — A single shared fact class containing all fields extracted across all uploaded documents, with:
  - Inferred Java types (field names containing `date` → `Date`, `amount`/`income` → `BigDecimal`, `age`/`count` → `Integer`, `is`/`has`/`valid` → `boolean`, everything else → `String`)
  - Getters and setters
  - A `ValidationResult` inner class and `addValidationResult()` method for collecting rule outcomes
  - An `isFullyValid()` convenience method

---

## Using the Web Interface

1. **Drag and drop** one or more `.docx` files onto the upload zone (or click to browse).
2. Optionally change the **Java package name** and **fact class name** in the settings fields.
3. Click **Convert to Drools DRL**.
4. Review the results:
   - **Rules Converted** — Number of OPA rules extracted.
   - **Files Generated** — Number of `.drl` + `.java` files produced.
   - **Items to Review** — Conditions that need manual attention (marked with `TODO`).
5. Expand any output card to preview the generated code. Use **Copy** or **Download** per file.
6. Click **Download All (.zip)** to get everything in one archive.

---

## Configuration Options

| Setting | Default | Description |
|---|---|---|
| Java Package Name | `com.rules.validation` | The `package` declaration in the generated `.drl` and `.java` files |
| Fact Class Name | `ValidationFact` | The name of the generated Java fact class |

---

## Limitations and Manual Review

The converter is optimized for **verification/validation rules** — rules that check whether fields are present, non-empty, and meet value criteria. Some situations require manual review after conversion:

- **Complex arithmetic or function calls** — OPA expressions involving calculations, date functions, or string manipulation are flagged with `TODO` comments.
- **Entity-level reasoning** — OPA rules that iterate over entity collections (e.g., "for all household members") are not auto-converted.
- **Rule tables** — OPA Word rule tables (tabular condition/conclusion grids) are not parsed; only paragraph-style rules are supported.
- **Non-English rules** — The pattern matching is English-only.
- **Type inference** — The auto-inferred Java types are a best guess from field names. Review and adjust them to match your actual domain model.

All items needing attention appear in the **Items to Review** count and as warning notes below the output cards.

---

## Project Structure

```
opa_to_drools_converter.py    # Single-file Flask application
├── OPARuleParser              # Parses .docx → extracts rule structures
├── DroolsDRLGenerator         # Converts parsed rules → .drl + .java
├── Flask routes               # Web UI and /convert API endpoint
│   ├── GET  /                 # Browser interface
│   ├── POST /convert          # Upload .docx, receive JSON with generated code
│   └── GET  /download/<id>    # Download .zip of generated files
└── HTML template              # Embedded single-page UI (no external dependencies)
```

---

## Troubleshooting

**"No rules found in document"**
The document may not use standard OPA Word styles. Check that paragraphs are styled with `OPM - conclusion`, `OPM - level 1`, etc. If the document is plain text, the fallback parser looks for lines ending in `if`/`when` followed by indented conditions.

**Fields have wrong Java types**
The type inferencer uses naming conventions. Rename fields in the generated fact class or adjust the `_infer_type()` method in the converter.

**Empty strings passing validation**
This was fixed — all presence checks now emit both `!= null` and `!= ""`. If you have older generated DRL files that only check `!= null`, re-convert the source documents.
