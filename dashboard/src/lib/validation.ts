// Mirrors the receiver's _EMAIL_RE so client and server agree. Requires a
// real TLD (2+ letters), so "a@b", "a@b.", and "a@b.c" are rejected.
const EMAIL_RE = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

export function validateEmail(email: string): string | null {
  if (!email) return "Email is required";
  if (email.length > 254) return "Email too long";
  if (!EMAIL_RE.test(email.trim())) return "Please enter a valid email address.";
  return null;
}

export function validatePassword(password: string): string | null {
  if (!password) return "Password is required";
  if (password.length < 8) return "Password must be at least 8 characters long.";
  if (password.length > 128) return "Password too long (max 128 characters)";
  if (!/[A-Za-z]/.test(password) || !/[0-9]/.test(password) || !/[^A-Za-z0-9]/.test(password)) {
    return "Please choose a secure password by combining letters, numbers, and special characters.";
  }
  return null;
}

/**
 * Per-rule status for the live requirement checklist shown under the
 * password field. Mirrors validatePassword so the inline UI and the
 * submit-time validation never disagree.
 */
export interface PasswordRule {
  label: string;
  met: boolean;
}
export function passwordRules(password: string): PasswordRule[] {
  return [
    { label: "At least 8 characters", met: password.length >= 8 },
    { label: "A letter", met: /[A-Za-z]/.test(password) },
    { label: "A number", met: /[0-9]/.test(password) },
    { label: "A special character", met: /[^A-Za-z0-9]/.test(password) },
  ];
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
