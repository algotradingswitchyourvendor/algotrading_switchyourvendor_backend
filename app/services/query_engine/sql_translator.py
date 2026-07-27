import logging
from typing import Any

logger = logging.getLogger(__name__)

def translate_conditions(conditions: list[dict]) -> str:
    """
    Translates a list of condition dictionaries into a DuckDB SQL WHERE clause.
    Returns '1=1' if no conditions or if they fail to parse.
    """
    if not conditions:
        return "1=1"
        
    where_parts = []
    
    for i, condition in enumerate(conditions):
        column = condition.get("column", "")
        operator = condition.get("operator", "=")
        value = condition.get("value")
        logical = condition.get("logical", "AND").upper()
        
        if not column:
            continue
            
        sql_part = _evaluate_single(column, operator, value)
        
        if sql_part:
            if i == 0:
                where_parts.append(sql_part)
            else:
                where_parts.append(f" {logical} {sql_part}")
                
    if not where_parts:
        return "1=1"
        
    return "".join(where_parts)

def _evaluate_single(column: str, operator: str, value: Any) -> str:
    """
    Translates a single condition to SQL string safely.
    DuckDB handles safe casting dynamically.
    """
    op = operator.lower().strip()
    
    # Safe quoting for column names
    col = f'"{column}"'
    
    # -- Null checks --
    if op == "is_null":
        return f"{col} IS NULL"
    if op == "is_not_null":
        return f"{col} IS NOT NULL"
        
    # Formatting values
    def format_val(v):
        if isinstance(v, (int, float)):
            return str(v)
        # DuckDB handles strings gracefully, quote and escape single quotes
        safe_str = str(v).replace("'", "''")
        return f"'{safe_str}'"
        
    # -- Numeric/Equality Operators --
    if op in (">", "<", ">=", "<=", "=", "!=", "=="):
        sql_op = "=" if op == "==" else op
        return f"{col} {sql_op} {format_val(value)}"
        
    if op == "between":
        if isinstance(value, list) and len(value) == 2:
            return f"{col} BETWEEN {format_val(value[0])} AND {format_val(value[1])}"
        return ""
        
    # -- String Operators --
    if op == "contains":
        safe_str = str(value).replace("'", "''")
        return f"{col} ILIKE '%{safe_str}%'"
        
    if op == "starts_with":
        safe_str = str(value).replace("'", "''")
        return f"{col} ILIKE '{safe_str}%'"
        
    if op == "ends_with":
        safe_str = str(value).replace("'", "''")
        return f"{col} ILIKE '%{safe_str}'"
        
    # -- Set Operators --
    if op == "in":
        if isinstance(value, list) and value:
            in_vals = ", ".join(format_val(v) for v in value)
            return f"{col} IN ({in_vals})"
        return "1=0" # IN empty list is false
        
    if op == "not_in":
        if isinstance(value, list) and value:
            in_vals = ", ".join(format_val(v) for v in value)
            return f"{col} NOT IN ({in_vals})"
        return "1=1"
        
    logger.warning(f"SQL Translator: unknown operator '{operator}'")
    return ""
