export function validateEmail(email: string): string | null {
  if (!email) return "Email is required";
  if (email.length > 254) return "Email too long";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return "Invalid email format";
  return null;
}

export function validatePassword(password: string): string | null {
  if (!password) return "Password is required";
  if (password.length < 10) return "Password must be at least 10 characters";
  if (password.length > 128) return "Password too long (max 128 characters)";
  return null;
}

export function validatePolicyName(name: string): string | null {
  if (!name) return "Name is required";
  if (name.length < 2) return "Name must be at least 2 characters";
  if (name.length > 200) return "Name too long (max 200 characters)";
  return null;
}

export function validateCEL(expression: string): string | null {
  if (!expression) return "Expression is required";
  if (expression.length > 10000) return "Expression too long";
  const openP = (expression.match(/\(/g) || []).length;
  const closeP = (expression.match(/\)/g) || []).length;
  if (openP !== closeP) return "Unmatched parentheses";
  return null;
}

export function sanitizeInput(input: string): string {
  return input.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "").replace(/[\u200B-\u200F\u2028-\u202F\uFEFF]/g, "").trim();
}
