"""
OPA-to-Drools Rule Converter
Flask web application that converts Oracle Policy Automation (OPA) Word rule documents
to Drools DRL (Drools Rule Language) files for use with a Java/Spring Boot Drools Engine.

Focused on verification/validation rules that check field presence and criteria.
"""

import os
import re
import io
import zipfile
import json
from datetime import datetime
from flask import Flask, request, render_template_string, send_file, jsonify
from docx import Document
from docx.oxml.ns import qn

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

UPLOAD_FOLDER = '/tmp/opa_uploads'
OUTPUT_FOLDER = '/tmp/opa_outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


# ─── OPA Document Parser ────────────────────────────────────────────────────────

class OPARuleParser:
    """
    Parses Oracle Policy Automation rule documents (.docx).
    OPA rules use specific Word styles:
      - "OPM - conclusion" or styles containing "conclusion" → rule conclusion
      - "OPM - level 1" / "Level 1" → first-level conditions
      - "OPM - level 2" / "Level 2" → sub-conditions (proving level 1)
      - "OPM - level 3" / "Level 3" → sub-sub-conditions
      - "OPM - rule name" → rule document/section name
    Conditions are connected by 'and' / 'or' operators.
    """

    # OPA style name patterns (case-insensitive matching)
    STYLE_PATTERNS = {
        'rule_name': [r'rule\s*name', r'opm.*rule.*name', r'heading\s*1'],
        'conclusion': [r'conclusion', r'opm.*conclusion'],
        'level1': [r'level\s*1', r'opm.*level\s*1'],
        'level2': [r'level\s*2', r'opm.*level\s*2'],
        'level3': [r'level\s*3', r'opm.*level\s*3'],
        'level4': [r'level\s*4', r'opm.*level\s*4'],
    }

    def __init__(self):
        self.rules = []
        self.rule_name = ""
        self.parse_warnings = []
        self.parse_info = []

    def classify_style(self, style_name):
        """Classify a Word paragraph style into an OPA role."""
        if not style_name:
            return None
        name_lower = style_name.lower().strip()
        for role, patterns in self.STYLE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, name_lower):
                    return role
        return None

    def classify_by_indent(self, paragraph):
        """
        Fallback classification by paragraph indentation level.
        OPA uses indentation to denote rule hierarchy when styles aren't standard.
        """
        pf = paragraph.paragraph_format
        left_indent = 0
        if pf.left_indent:
            left_indent = pf.left_indent.pt if hasattr(pf.left_indent, 'pt') else 0

        # Also check for tab-based indentation
        text = paragraph.text
        tab_count = len(text) - len(text.lstrip('\t'))

        indent_level = max(tab_count, int(left_indent / 36))  # ~36pt per indent level

        if indent_level == 0:
            return 'conclusion'
        elif indent_level == 1:
            return 'level1'
        elif indent_level == 2:
            return 'level2'
        elif indent_level >= 3:
            return 'level3'
        return None

    def parse_document(self, docx_path):
        """Parse an OPA .docx file and extract rules."""
        doc = Document(docx_path)
        self.rules = []
        self.rule_name = ""
        self.parse_warnings = []
        self.parse_info = []

        raw_lines = []
        style_detected = False

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""
            role = self.classify_style(style_name)

            if role:
                style_detected = True
            raw_lines.append({
                'text': text,
                'style': style_name,
                'role': role,
                'paragraph': para
            })

        # If no OPA styles detected, try indent-based parsing
        if not style_detected:
            self.parse_warnings.append(
                "No OPA-specific styles detected. Using indentation-based parsing. "
                "Results may need manual review."
            )
            for line in raw_lines:
                if not line['role']:
                    line['role'] = self.classify_by_indent(line['paragraph'])

        # Also try to detect rules from plain text patterns
        if not style_detected:
            self._parse_plain_text(raw_lines)
        else:
            self._parse_styled(raw_lines)

        self.parse_info.append(f"Extracted {len(self.rules)} rule(s) from document.")
        return self.rules

    def _parse_plain_text(self, lines):
        """
        Parse rules from plain text when no OPA styles are detected.
        Looks for patterns like:
          "X if" / "X when" → conclusion
          Indented lines → conditions
        Also handles simple validation statements.
        """
        current_rule = None

        for line in lines:
            text = line['text']

            # Detect conclusion patterns: "something if" at end
            if re.search(r'\b(if|when)\s*$', text, re.IGNORECASE):
                if current_rule:
                    self.rules.append(current_rule)
                conclusion_text = re.sub(r'\s*(if|when)\s*$', '', text, flags=re.IGNORECASE).strip()
                current_rule = {
                    'conclusion': conclusion_text,
                    'conditions': [],
                    'operator': 'and',
                    'raw_text': text
                }
            elif current_rule:
                # Check if it's an operator line
                text_lower = text.lower().strip()
                if text_lower in ('and', 'or'):
                    current_rule['operator'] = text_lower
                else:
                    # It's a condition
                    current_rule['conditions'].append({
                        'text': text,
                        'level': line.get('role', 'level1') or 'level1',
                        'sub_conditions': []
                    })
            else:
                # Standalone statement - treat as a simple validation rule
                if any(kw in text.lower() for kw in [
                    'must', 'required', 'valid', 'should', 'is not null',
                    'is present', 'exists', 'is not empty', 'is provided',
                    'is known', 'has been provided', 'has a value'
                ]):
                    self.rules.append({
                        'conclusion': text,
                        'conditions': [],
                        'operator': 'and',
                        'raw_text': text,
                        'is_simple_validation': True
                    })

        if current_rule:
            self.rules.append(current_rule)

    def _parse_styled(self, lines):
        """Parse rules using detected OPA styles."""
        current_rule = None
        current_l1_condition = None

        for line in lines:
            role = line['role']
            text = line['text']

            if role == 'rule_name':
                self.rule_name = text
                continue

            if role == 'conclusion':
                if current_rule:
                    self.rules.append(current_rule)
                # Strip trailing "if"/"when"
                conclusion_text = re.sub(r'\s*(if|when)\s*$', '', text, flags=re.IGNORECASE).strip()
                current_rule = {
                    'conclusion': conclusion_text,
                    'conditions': [],
                    'operator': 'and',
                    'raw_text': text
                }
                current_l1_condition = None

            elif role == 'level1' and current_rule:
                text_lower = text.lower().strip()
                if text_lower in ('and', 'or'):
                    current_rule['operator'] = text_lower
                elif text_lower.startswith('and ') or text_lower.startswith('or '):
                    op = text_lower.split()[0]
                    current_rule['operator'] = op
                    cond_text = text[len(op):].strip()
                    cond = {'text': cond_text, 'level': 'level1', 'sub_conditions': [], 'sub_operator': 'and'}
                    current_rule['conditions'].append(cond)
                    current_l1_condition = cond
                else:
                    cond = {'text': text, 'level': 'level1', 'sub_conditions': [], 'sub_operator': 'and'}
                    current_rule['conditions'].append(cond)
                    current_l1_condition = cond

            elif role in ('level2', 'level3', 'level4') and current_l1_condition:
                text_lower = text.lower().strip()
                if text_lower in ('and', 'or'):
                    current_l1_condition['sub_operator'] = text_lower
                else:
                    # Strip leading and/or
                    cond_text = text
                    if text_lower.startswith('and ') or text_lower.startswith('or '):
                        op = text_lower.split()[0]
                        current_l1_condition['sub_operator'] = op
                        cond_text = text[len(op):].strip()
                    current_l1_condition['sub_conditions'].append({
                        'text': cond_text,
                        'level': role
                    })

        if current_rule:
            self.rules.append(current_rule)


# ─── Drools DRL Generator ───────────────────────────────────────────────────────

class DroolsDRLGenerator:
    """
    Generates Drools DRL files from parsed OPA rules.
    Focused on verification/validation rules that check:
      - Field presence (not null, not empty)
      - Field values meeting criteria (comparisons, patterns)
      - Boolean conditions
    """

    # Common OPA verification phrase → Drools condition mapping
    VERIFICATION_PATTERNS = [
        # Null/presence checks — also reject empty strings ("")
        # In OPA "is known" / "is provided" / "is not null" means the value is
        # meaningfully present, so we check both != null AND != "" for Strings.
        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+)?(?:not\s+null|is\s+known|is\s+provided|has\s+been\s+provided|has\s+a\s+value|is\s+present|exists|is\s+not\s+empty)",
         lambda m: f'{_to_field(m.group(1))} != null, {_to_field(m.group(1))} != ""'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+)?(?:null|is\s+unknown|is\s+not\s+provided|is\s+not\s+known|is\s+empty|is\s+missing|does\s+not\s+exist)",
         lambda m: f'{_to_field(m.group(1))} == null || {_to_field(m.group(1))} == ""'),

        # Comparison operators
        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+)?(?:greater\s+than|more\s+than|above|exceeds|>)\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} > {m.group(2)}'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+)?(?:less\s+than|below|under|<)\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} < {m.group(2)}'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+)?(?:greater\s+than\s+or\s+equal\s+to|at\s+least|>=)\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} >= {m.group(2)}'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+)?(?:less\s+than\s+or\s+equal\s+to|at\s+most|no\s+more\s+than|<=)\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} <= {m.group(2)}'),

        # Equality
        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+equal\s+to|equals|is)\s+\"([^\"]+)\"",
         lambda m: f'{_to_field(m.group(1))} == "{m.group(2)}"'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+equal\s+to|equals|is)\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} == {m.group(2)}'),

        # Not equal
        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+not\s+equal\s+to|does\s+not\s+equal|is\s+not|!=)\s+\"([^\"]+)\"",
         lambda m: f'{_to_field(m.group(1))} != "{m.group(2)}"'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+(?:is\s+not\s+equal\s+to|does\s+not\s+equal|is\s+not|!=)\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} != {m.group(2)}'),

        # Boolean states
        (r"(?:the\s+)?(\w[\w\s]*?)\s+is\s+true",
         lambda m: f'{_to_field(m.group(1))} == true'),

        (r"(?:the\s+)?(\w[\w\s]*?)\s+is\s+false",
         lambda m: f'{_to_field(m.group(1))} == false'),

        # Contains / matches
        (r"(?:the\s+)?(\w[\w\s]*?)\s+contains\s+\"([^\"]+)\"",
         lambda m: f'{_to_field(m.group(1))} contains "{m.group(2)}"'),

        # Between
        (r"(?:the\s+)?(\w[\w\s]*?)\s+is\s+between\s+([\d.]+)\s+and\s+([\d.]+)",
         lambda m: f'{_to_field(m.group(1))} >= {m.group(2)} && {_to_field(m.group(1))} <= {m.group(3)}'),

        # Length checks
        (r"(?:the\s+length\s+of\s+)?(?:the\s+)?(\w[\w\s]*?)\s+(?:length\s+)?(?:is\s+)?(?:greater\s+than|more\s+than|>)\s+(\d+)",
         lambda m: f'{_to_field(m.group(1))}.length() > {m.group(2)}'),

        (r"(?:the\s+length\s+of\s+)?(?:the\s+)?(\w[\w\s]*?)\s+(?:length\s+)?(?:is\s+)?(?:exactly|==)\s+(\d+)",
         lambda m: f'{_to_field(m.group(1))}.length() == {m.group(2)}'),
    ]

    def __init__(self, package_name="com.rules.validation", entity_name=None):
        self.package_name = package_name
        self.entity_name = entity_name or "ValidationFact"
        self.import_classes = set()
        self.generated_fields = set()
        self.conversion_notes = []

    def generate_drl(self, rules, source_filename=""):
        """Generate a complete DRL file from parsed OPA rules."""
        drl_rules = []
        rule_counter = 0

        for rule in rules:
            rule_counter += 1
            rule_name = self._make_rule_name(rule, rule_counter)
            drl_rule = self._convert_rule(rule, rule_name)
            if drl_rule:
                drl_rules.append(drl_rule)

        # Build the full DRL file
        drl_content = self._build_drl_file(drl_rules, source_filename)
        fact_class = self._generate_fact_class()
        return drl_content, fact_class

    def _make_rule_name(self, rule, counter):
        """Create a Drools-compatible rule name."""
        conclusion = rule.get('conclusion', f'Rule {counter}')
        # Clean up for rule name
        name = re.sub(r'[^a-zA-Z0-9\s]', '', conclusion)
        name = ' '.join(name.split()[:8])  # Limit length
        if not name:
            name = f"Validation Rule {counter}"
        return f"{name} - Rule {counter}"

    def _convert_condition_text(self, text):
        """Convert an OPA condition text to a Drools constraint expression."""
        text = text.strip()

        # Try each verification pattern
        for pattern, converter in self.VERIFICATION_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                result = converter(match)
                # Track the field
                field_match = re.search(r'^(\w+)', result)
                if field_match:
                    self.generated_fields.add(field_match.group(1))
                return result

        # Fallback: convert natural language to a commented constraint
        field = _to_field(text)
        self.generated_fields.add(field)
        self.conversion_notes.append(
            f"MANUAL REVIEW NEEDED: Could not auto-convert condition: \"{text}\" → using field '{field}'"
        )
        return f'/* TODO: Review - "{text}" */ {field} != null, {field} != ""'

    def _convert_rule(self, rule, rule_name):
        """Convert a single OPA rule to a Drools DRL rule string."""
        conclusion = rule.get('conclusion', '')
        conditions = rule.get('conditions', [])
        operator = rule.get('operator', 'and')
        is_simple = rule.get('is_simple_validation', False)

        drools_conditions = []

        if is_simple or not conditions:
            # Simple validation statement - parse the conclusion itself as a condition
            cond = self._convert_condition_text(conclusion)
            drools_conditions.append(cond)
        else:
            for cond in conditions:
                cond_expr = self._convert_condition_text(cond['text'])

                # Handle sub-conditions
                if cond.get('sub_conditions'):
                    sub_exprs = []
                    for sub in cond['sub_conditions']:
                        sub_expr = self._convert_condition_text(sub['text'])
                        sub_exprs.append(sub_expr)
                    sub_op = f" {'&&' if cond.get('sub_operator', 'and') == 'and' else '||'} "
                    combined = sub_op.join(sub_exprs)
                    cond_expr = f"({cond_expr} && ({combined}))" if sub_exprs else cond_expr

                drools_conditions.append(cond_expr)

        # Build the when clause
        # In Drools, constraints inside a pattern are separated by commas (implicit AND)
        # or by || for OR conditions
        if operator == 'and':
            when_clause = ",\n            ".join(drools_conditions)
        elif operator == 'or' and len(drools_conditions) > 1:
            # For OR, each condition may itself be a multi-part check (e.g. "!= null, != """)
            # We need to wrap each multi-part condition in parentheses and join with ||
            or_parts = []
            for cond in drools_conditions:
                # If the condition contains commas (multiple constraints), wrap in parens
                if ',' in cond:
                    # Convert comma-separated AND constraints to && for use inside || expression
                    sub_parts = [p.strip() for p in cond.split(',')]
                    or_parts.append('(' + ' && '.join(sub_parts) + ')')
                else:
                    or_parts.append(cond)
            when_clause = " ||\n            ".join(or_parts)
        else:
            when_clause = drools_conditions[0] if drools_conditions else "/* no conditions */"

        # Build the conclusion/action
        conclusion_field = _to_field(conclusion)
        self.generated_fields.add(conclusion_field)

        # Determine the action
        if is_simple:
            action = self._build_validation_action(conclusion, conclusion_field)
        else:
            action = self._build_conclusion_action(conclusion, conclusion_field)

        drl = f'''rule "{rule_name}"
    dialect "mvel"
    when
        $fact : {self.entity_name}(
            {when_clause}
        )
    then
        {action}
end'''
        return drl

    def _build_validation_action(self, conclusion_text, field_name):
        """Build a then-action for simple validation rules."""
        return (
            f'// Validation: {conclusion_text}\n'
            f'        $fact.addValidationResult("{field_name}", true, "{_escape_drl(conclusion_text)}");'
        )

    def _build_conclusion_action(self, conclusion_text, field_name):
        """Build a then-action for conditional rules."""
        return (
            f'// Conclusion: {conclusion_text}\n'
            f'        $fact.set{_to_class_name(field_name)}(true);\n'
            f'        $fact.addValidationResult("{field_name}", true, "{_escape_drl(conclusion_text)}");'
        )

    def _build_drl_file(self, drl_rules, source_filename):
        """Assemble the complete DRL file content."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"""package {self.package_name};

// ============================================================================
// AUTO-GENERATED Drools DRL Rules
// Converted from Oracle Policy Automation (OPA) rule document
// Source: {source_filename}
// Generated: {timestamp}
//
// IMPORTANT: Review all rules marked with TODO comments.
// Ensure the {self.entity_name} fact class matches your domain model.
// ============================================================================

import {self.package_name}.{self.entity_name};
import java.util.Date;
import java.math.BigDecimal;

"""
        rules_section = "\n\n".join(drl_rules)

        # Add conversion notes as comments
        notes = ""
        if self.conversion_notes:
            notes = "\n// ============================================================================\n"
            notes += "// CONVERSION NOTES - Items requiring manual review:\n"
            for i, note in enumerate(self.conversion_notes, 1):
                notes += f"// {i}. {note}\n"
            notes += "// ============================================================================\n\n"

        return header + notes + rules_section + "\n"

    def _generate_fact_class(self):
        """Generate a Java fact class based on extracted fields."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        fields_code = ""
        getters_setters = ""
        for field in sorted(self.generated_fields):
            java_type = self._infer_type(field)
            fields_code += f"    private {java_type} {field};\n"
            getter_name = f"get{_to_class_name(field)}"
            setter_name = f"set{_to_class_name(field)}"
            if java_type == "boolean":
                getter_name = f"is{_to_class_name(field)}"

            getters_setters += f"""
    public {java_type} {getter_name}() {{
        return this.{field};
    }}

    public void {setter_name}({java_type} {field}) {{
        this.{field} = {field};
    }}
"""

        java_class = f"""package {self.package_name};

// ============================================================================
// AUTO-GENERATED Fact Class for Drools Validation Rules
// Generated: {timestamp}
//
// TODO: Map these fields to your actual database entity fields.
// This is a scaffold - adjust types, names and add your JPA annotations.
// ============================================================================

import java.util.Date;
import java.util.List;
import java.util.ArrayList;
import java.util.Map;
import java.util.HashMap;
import java.math.BigDecimal;

public class {self.entity_name} {{

    // Validation results storage
    private List<ValidationResult> validationResults = new ArrayList<>();
    private Map<String, Boolean> validationFlags = new HashMap<>();

    // === Extracted Fields (review and adjust types) ===
{fields_code}
    // === Constructors ===

    public {self.entity_name}() {{
    }}

    // === Validation Result Methods ===

    public void addValidationResult(String field, boolean passed, String message) {{
        this.validationResults.add(new ValidationResult(field, passed, message));
        this.validationFlags.put(field, passed);
    }}

    public List<ValidationResult> getValidationResults() {{
        return this.validationResults;
    }}

    public boolean isFullyValid() {{
        return this.validationFlags.values().stream().allMatch(v -> v);
    }}

    public Map<String, Boolean> getValidationFlags() {{
        return this.validationFlags;
    }}

    // === Getters and Setters ===
{getters_setters}
    // === Inner class for validation results ===

    public static class ValidationResult {{
        private String fieldName;
        private boolean passed;
        private String message;
        private Date timestamp;

        public ValidationResult(String fieldName, boolean passed, String message) {{
            this.fieldName = fieldName;
            this.passed = passed;
            this.message = message;
            this.timestamp = new Date();
        }}

        public String getFieldName() {{ return fieldName; }}
        public boolean isPassed() {{ return passed; }}
        public String getMessage() {{ return message; }}
        public Date getTimestamp() {{ return timestamp; }}

        @Override
        public String toString() {{
            return String.format("[%s] %s: %s - %s",
                passed ? "PASS" : "FAIL", fieldName, message,
                timestamp.toString());
        }}
    }}
}}
"""
        return java_class

    def _infer_type(self, field_name):
        """Infer a Java type from the field name."""
        name_lower = field_name.lower()
        if any(kw in name_lower for kw in ['date', 'time', 'dob', 'birth']):
            return "Date"
        if any(kw in name_lower for kw in ['amount', 'salary', 'price', 'cost', 'rate', 'balance', 'income', 'total']):
            return "BigDecimal"
        if any(kw in name_lower for kw in ['count', 'number', 'age', 'years', 'quantity', 'num']):
            return "Integer"
        if any(kw in name_lower for kw in ['is', 'has', 'eligible', 'valid', 'active', 'approved', 'required', 'verified']):
            return "boolean"
        return "String"


# ─── Utility Functions ───────────────────────────────────────────────────────────

def _to_field(text):
    """Convert natural language text to a camelCase Java field name."""
    # Remove common OPA prefixes
    text = re.sub(r'^(the|a|an)\s+', '', text.strip(), flags=re.IGNORECASE)
    # Remove trailing verbs/helpers
    text = re.sub(r'\s+(is|are|was|were|has|have|had|does|do|did|if|when|then)$', '', text, flags=re.IGNORECASE)
    # Extract possessive patterns: "the person's name" → "personName"
    text = re.sub(r"(\w+)'s\s+", r'\1 ', text)
    # Remove non-alphanumeric
    text = re.sub(r'[^a-zA-Z0-9\s]', '', text)
    # Convert to camelCase
    words = text.strip().split()
    if not words:
        return "unknownField"
    result = words[0].lower() + ''.join(w.capitalize() for w in words[1:])
    # Ensure valid Java identifier
    if result[0].isdigit():
        result = 'field' + result
    return result


def _to_class_name(field_name):
    """Convert a field name to PascalCase for getter/setter."""
    if not field_name:
        return "Unknown"
    return field_name[0].upper() + field_name[1:]


def _escape_drl(text):
    """Escape a string for use in DRL string literals."""
    return text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


# ─── Flask Routes ────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OPA → Drools Rule Converter</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0f1117;
            --bg-secondary: #161922;
            --bg-tertiary: #1c2030;
            --bg-card: #1a1e2e;
            --border: #2a2f42;
            --border-hover: #3d4460;
            --text-primary: #e8eaf0;
            --text-secondary: #9499b0;
            --text-muted: #5e6380;
            --accent: #6c8aff;
            --accent-hover: #8aa3ff;
            --accent-glow: rgba(108, 138, 255, 0.15);
            --success: #3dd68c;
            --success-bg: rgba(61, 214, 140, 0.08);
            --warning: #f0a030;
            --warning-bg: rgba(240, 160, 48, 0.08);
            --error: #f06060;
            --error-bg: rgba(240, 96, 96, 0.08);
            --oracle-red: #c74634;
            --drools-blue: #6c8aff;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: 'DM Sans', system-ui, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.6;
        }

        .app-container {
            max-width: 960px;
            margin: 0 auto;
            padding: 40px 24px;
        }

        /* Header */
        .header {
            text-align: center;
            margin-bottom: 48px;
        }

        .header-badge {
            display: inline-flex;
            align-items: center;
            gap: 12px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            color: var(--text-secondary);
            margin-bottom: 20px;
            padding: 6px 16px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: 20px;
        }

        .badge-dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            background: var(--success);
            animation: pulse 2s ease-in-out infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        .header h1 {
            font-size: 36px;
            font-weight: 700;
            letter-spacing: -0.5px;
            margin-bottom: 8px;
        }

        .header h1 .oracle { color: var(--oracle-red); }
        .header h1 .arrow { color: var(--text-muted); margin: 0 4px; }
        .header h1 .drools { color: var(--drools-blue); }

        .header p {
            color: var(--text-secondary);
            font-size: 15px;
            max-width: 520px;
            margin: 0 auto;
        }

        /* Upload Zone */
        .upload-zone {
            border: 2px dashed var(--border);
            border-radius: 16px;
            padding: 48px 32px;
            text-align: center;
            cursor: pointer;
            transition: all 0.25s ease;
            background: var(--bg-secondary);
            position: relative;
            overflow: hidden;
        }

        .upload-zone:hover, .upload-zone.dragover {
            border-color: var(--accent);
            background: var(--accent-glow);
        }

        .upload-zone.dragover {
            transform: scale(1.01);
        }

        .upload-icon {
            width: 56px; height: 56px;
            margin: 0 auto 16px;
            border-radius: 14px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
        }

        .upload-zone h3 {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 6px;
        }

        .upload-zone p {
            font-size: 13px;
            color: var(--text-muted);
        }

        .upload-zone input[type="file"] {
            position: absolute;
            inset: 0;
            opacity: 0;
            cursor: pointer;
        }

        /* Settings */
        .settings-panel {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin: 24px 0;
        }

        .setting-group label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 6px;
        }

        .setting-group input {
            width: 100%;
            padding: 10px 14px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            transition: border-color 0.2s;
        }

        .setting-group input:focus {
            outline: none;
            border-color: var(--accent);
        }

        /* File list */
        .file-list {
            margin: 20px 0;
        }

        .file-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 16px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: 10px;
            margin-bottom: 8px;
            animation: slideIn 0.3s ease;
        }

        @keyframes slideIn {
            from { opacity: 0; transform: translateY(-8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .file-item-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .file-icon {
            width: 36px; height: 36px;
            border-radius: 8px;
            background: rgba(108, 138, 255, 0.1);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
        }

        .file-name {
            font-size: 14px;
            font-weight: 500;
        }

        .file-size {
            font-size: 12px;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
        }

        .file-remove {
            background: none;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            font-size: 18px;
            padding: 4px 8px;
            border-radius: 6px;
            transition: all 0.2s;
        }

        .file-remove:hover {
            color: var(--error);
            background: var(--error-bg);
        }

        /* Convert button */
        .convert-btn {
            width: 100%;
            padding: 14px 24px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 10px;
            font-family: 'DM Sans', sans-serif;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.25s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }

        .convert-btn:hover:not(:disabled) {
            background: var(--accent-hover);
            transform: translateY(-1px);
            box-shadow: 0 4px 20px rgba(108, 138, 255, 0.3);
        }

        .convert-btn:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }

        .convert-btn.loading {
            pointer-events: none;
        }

        .spinner {
            width: 18px; height: 18px;
            border: 2px solid rgba(255,255,255,0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: spin 0.7s linear infinite;
        }

        @keyframes spin { to { transform: rotate(360deg); } }

        /* Results */
        .results-section {
            margin-top: 32px;
            animation: fadeIn 0.5s ease;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .result-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
        }

        .result-header h2 {
            font-size: 20px;
            font-weight: 600;
        }

        .download-all-btn {
            padding: 8px 20px;
            background: var(--success);
            color: #0f1117;
            border: none;
            border-radius: 8px;
            font-family: 'DM Sans', sans-serif;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .download-all-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 16px rgba(61, 214, 140, 0.3);
        }

        /* Stats */
        .stats-row {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 24px;
        }

        .stat-card {
            padding: 16px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 10px;
            text-align: center;
        }

        .stat-value {
            font-size: 28px;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }

        .stat-value.success { color: var(--success); }
        .stat-value.warning { color: var(--warning); }
        .stat-value.info { color: var(--accent); }

        .stat-label {
            font-size: 11px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 4px;
        }

        /* Output cards */
        .output-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 16px;
            overflow: hidden;
        }

        .output-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 14px 20px;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border);
            cursor: pointer;
            user-select: none;
        }

        .output-card-header:hover {
            background: var(--bg-card);
        }

        .output-card-title {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
            font-weight: 600;
        }

        .output-type-badge {
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
        }

        .badge-drl {
            background: rgba(108, 138, 255, 0.15);
            color: var(--accent);
        }

        .badge-java {
            background: rgba(240, 160, 48, 0.15);
            color: var(--warning);
        }

        .output-card-actions {
            display: flex;
            gap: 8px;
        }

        .btn-sm {
            padding: 5px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            border: 1px solid var(--border);
            background: var(--bg-secondary);
            color: var(--text-secondary);
            cursor: pointer;
            font-family: 'DM Sans', sans-serif;
            transition: all 0.2s;
        }

        .btn-sm:hover {
            border-color: var(--accent);
            color: var(--accent);
        }

        .output-card-body {
            display: none;
        }

        .output-card-body.open {
            display: block;
        }

        .code-preview {
            padding: 20px;
            max-height: 420px;
            overflow: auto;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12.5px;
            line-height: 1.7;
            white-space: pre-wrap;
            color: var(--text-secondary);
            background: var(--bg-primary);
        }

        /* Notes/warnings */
        .notes-section {
            margin-top: 20px;
        }

        .note-item {
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 13px;
            margin-bottom: 8px;
            display: flex;
            align-items: flex-start;
            gap: 10px;
        }

        .note-item.info {
            background: var(--accent-glow);
            border: 1px solid rgba(108, 138, 255, 0.2);
            color: var(--accent);
        }

        .note-item.warning {
            background: var(--warning-bg);
            border: 1px solid rgba(240, 160, 48, 0.2);
            color: var(--warning);
        }

        .note-item.error {
            background: var(--error-bg);
            border: 1px solid rgba(240, 96, 96, 0.2);
            color: var(--error);
        }

        .note-icon { font-size: 16px; flex-shrink: 0; margin-top: 1px; }

        /* Scrollbar */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--border-hover); }

        @media (max-width: 640px) {
            .settings-panel { grid-template-columns: 1fr; }
            .stats-row { grid-template-columns: 1fr; }
            .header h1 { font-size: 24px; }
        }
    </style>
</head>
<body>
    <div class="app-container">
        <header class="header">
            <div class="header-badge">
                <span class="badge-dot"></span>
                Rule Converter v1.0
            </div>
            <h1>
                <span class="oracle">OPA</span>
                <span class="arrow">→</span>
                <span class="drools">Drools</span>
            </h1>
            <p>Upload Oracle Policy Automation .docx rule documents and convert them to Drools DRL files for your Java/Spring Boot engine.</p>
        </header>

        <form id="convertForm" enctype="multipart/form-data">
            <div class="upload-zone" id="uploadZone">
                <div class="upload-icon">📄</div>
                <h3>Drop .docx files here or click to browse</h3>
                <p>Supports multiple OPA rule documents</p>
                <input type="file" id="fileInput" name="files" multiple accept=".docx,.doc">
            </div>

            <div class="file-list" id="fileList"></div>

            <div class="settings-panel">
                <div class="setting-group">
                    <label>Java Package Name</label>
                    <input type="text" id="packageName" name="package_name" value="com.rules.validation" placeholder="com.rules.validation">
                </div>
                <div class="setting-group">
                    <label>Fact Class Name</label>
                    <input type="text" id="entityName" name="entity_name" value="ValidationFact" placeholder="ValidationFact">
                </div>
            </div>

            <button type="submit" class="convert-btn" id="convertBtn" disabled>
                Convert to Drools DRL
            </button>
        </form>

        <div id="results"></div>
    </div>

    <script>
        const fileInput = document.getElementById('fileInput');
        const uploadZone = document.getElementById('uploadZone');
        const fileList = document.getElementById('fileList');
        const convertBtn = document.getElementById('convertBtn');
        const form = document.getElementById('convertForm');
        let selectedFiles = [];

        // Drag & drop
        uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
        uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
        uploadZone.addEventListener('drop', e => {
            e.preventDefault();
            uploadZone.classList.remove('dragover');
            handleFiles(e.dataTransfer.files);
        });

        fileInput.addEventListener('change', () => handleFiles(fileInput.files));

        function handleFiles(files) {
            for (const f of files) {
                if (f.name.endsWith('.docx') || f.name.endsWith('.doc')) {
                    if (!selectedFiles.find(sf => sf.name === f.name)) {
                        selectedFiles.push(f);
                    }
                }
            }
            renderFileList();
        }

        function renderFileList() {
            fileList.innerHTML = '';
            selectedFiles.forEach((f, i) => {
                const size = f.size < 1024 ? f.size + ' B' :
                             f.size < 1048576 ? (f.size/1024).toFixed(1) + ' KB' :
                             (f.size/1048576).toFixed(1) + ' MB';
                fileList.innerHTML += `
                    <div class="file-item">
                        <div class="file-item-left">
                            <div class="file-icon">📑</div>
                            <div>
                                <div class="file-name">${f.name}</div>
                                <div class="file-size">${size}</div>
                            </div>
                        </div>
                        <button class="file-remove" onclick="removeFile(${i})">×</button>
                    </div>`;
            });
            convertBtn.disabled = selectedFiles.length === 0;
        }

        function removeFile(i) {
            selectedFiles.splice(i, 1);
            renderFileList();
        }

        form.addEventListener('submit', async e => {
            e.preventDefault();
            if (!selectedFiles.length) return;

            convertBtn.innerHTML = '<div class="spinner"></div> Converting...';
            convertBtn.classList.add('loading');

            const fd = new FormData();
            selectedFiles.forEach(f => fd.append('files', f));
            fd.append('package_name', document.getElementById('packageName').value);
            fd.append('entity_name', document.getElementById('entityName').value);

            try {
                const resp = await fetch('/convert', { method: 'POST', body: fd });
                const data = await resp.json();

                if (data.error) {
                    document.getElementById('results').innerHTML = `
                        <div class="results-section">
                            <div class="note-item error">
                                <span class="note-icon">⚠</span>
                                <span>${data.error}</span>
                            </div>
                        </div>`;
                } else {
                    renderResults(data);
                }
            } catch (err) {
                document.getElementById('results').innerHTML = `
                    <div class="results-section">
                        <div class="note-item error">
                            <span class="note-icon">⚠</span>
                            <span>Connection error: ${err.message}</span>
                        </div>
                    </div>`;
            } finally {
                convertBtn.innerHTML = 'Convert to Drools DRL';
                convertBtn.classList.remove('loading');
            }
        });

        function renderResults(data) {
            const r = data;
            let html = '<div class="results-section">';

            // Header
            html += `<div class="result-header">
                <h2>✓ Conversion Complete</h2>
                <a href="/download/${r.zip_id}" class="download-all-btn">⬇ Download All (.zip)</a>
            </div>`;

            // Stats
            html += `<div class="stats-row">
                <div class="stat-card">
                    <div class="stat-value success">${r.total_rules}</div>
                    <div class="stat-label">Rules Converted</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value info">${r.total_files}</div>
                    <div class="stat-label">Files Generated</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value warning">${r.review_items}</div>
                    <div class="stat-label">Items to Review</div>
                </div>
            </div>`;

            // Output files
            r.outputs.forEach((out, idx) => {
                const ext = out.filename.endsWith('.drl') ? 'drl' : 'java';
                const badge = ext === 'drl' ? 'badge-drl' : 'badge-java';
                html += `
                <div class="output-card">
                    <div class="output-card-header" onclick="toggleCard(${idx})">
                        <div class="output-card-title">
                            <span class="output-type-badge ${badge}">.${ext}</span>
                            ${out.filename}
                        </div>
                        <div class="output-card-actions">
                            <button class="btn-sm" onclick="event.stopPropagation(); copyCode(${idx})">Copy</button>
                            <button class="btn-sm" onclick="event.stopPropagation(); downloadSingle('${out.filename}', ${idx})">Download</button>
                        </div>
                    </div>
                    <div class="output-card-body" id="cardBody${idx}">
                        <pre class="code-preview" id="code${idx}">${escapeHtml(out.content)}</pre>
                    </div>
                </div>`;
            });

            // Notes
            if (r.warnings.length || r.info.length || r.conversion_notes.length) {
                html += '<div class="notes-section">';
                r.warnings.forEach(w => {
                    html += `<div class="note-item warning"><span class="note-icon">⚡</span><span>${escapeHtml(w)}</span></div>`;
                });
                r.conversion_notes.forEach(n => {
                    html += `<div class="note-item warning"><span class="note-icon">🔧</span><span>${escapeHtml(n)}</span></div>`;
                });
                r.info.forEach(i => {
                    html += `<div class="note-item info"><span class="note-icon">ℹ</span><span>${escapeHtml(i)}</span></div>`;
                });
                html += '</div>';
            }

            html += '</div>';
            document.getElementById('results').innerHTML = html;
        }

        function toggleCard(idx) {
            document.getElementById('cardBody' + idx).classList.toggle('open');
        }

        function copyCode(idx) {
            const text = document.getElementById('code' + idx).textContent;
            navigator.clipboard.writeText(text);
        }

        function downloadSingle(filename, idx) {
            const text = document.getElementById('code' + idx).textContent;
            const blob = new Blob([text], { type: 'text/plain' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = filename;
            a.click();
        }

        function escapeHtml(s) {
            return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/convert', methods=['POST'])
def convert():
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    package_name = request.form.get('package_name', 'com.rules.validation').strip()
    entity_name = request.form.get('entity_name', 'ValidationFact').strip()

    if not re.match(r'^[a-zA-Z][\w.]*$', package_name):
        package_name = 'com.rules.validation'
    if not re.match(r'^[A-Z]\w*$', entity_name):
        entity_name = 'ValidationFact'

    all_outputs = []
    all_warnings = []
    all_info = []
    all_notes = []
    total_rules = 0

    parser = OPARuleParser()
    generator = DroolsDRLGenerator(package_name=package_name, entity_name=entity_name)

    for file in files:
        if not file.filename:
            continue

        # Save uploaded file
        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)

        try:
            rules = parser.parse_document(filepath)
            all_warnings.extend(parser.parse_warnings)
            all_info.extend(parser.parse_info)

            if not rules:
                all_warnings.append(f"No rules found in {file.filename}. Check that the document uses OPA styles or standard rule format.")
                continue

            total_rules += len(rules)

            # Generate DRL
            base_name = os.path.splitext(file.filename)[0]
            drl_filename = f"{_to_drl_name(base_name)}.drl"
            drl_content, fact_class = generator.generate_drl(rules, file.filename)

            all_outputs.append({
                'filename': drl_filename,
                'content': drl_content
            })

            all_notes.extend(generator.conversion_notes)
            generator.conversion_notes = []  # Reset for next file

        except Exception as e:
            all_warnings.append(f"Error processing {file.filename}: {str(e)}")
        finally:
            os.remove(filepath)

    # Add the fact class (once, combining all fields)
    if all_outputs:
        fact_class = generator._generate_fact_class()
        all_outputs.append({
            'filename': f"{entity_name}.java",
            'content': fact_class
        })

    # Create ZIP
    zip_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(OUTPUT_FOLDER, f"drools_rules_{zip_id}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for out in all_outputs:
            zf.writestr(out['filename'], out['content'])

    return jsonify({
        'outputs': all_outputs,
        'total_rules': total_rules,
        'total_files': len(all_outputs),
        'review_items': len(all_notes),
        'warnings': all_warnings,
        'info': all_info,
        'conversion_notes': all_notes,
        'zip_id': zip_id
    })


@app.route('/download/<zip_id>')
def download(zip_id):
    zip_path = os.path.join(OUTPUT_FOLDER, f"drools_rules_{zip_id}.zip")
    if os.path.exists(zip_path):
        return send_file(zip_path, as_attachment=True, download_name=f"drools_rules_{zip_id}.zip")
    return "File not found", 404


def _to_drl_name(name):
    """Convert a document name to a valid DRL filename."""
    name = re.sub(r'[^a-zA-Z0-9_\-]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name or "rules"


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
