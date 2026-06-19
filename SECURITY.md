# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
responsibly. **Do not open a public GitHub issue for security vulnerabilities.**

### How to Report

Email: harisiftikhar580@gmail.com
Subject line: `[SECURITY] RAG Customer Support Agent - <brief description>`

Please include:
- A description of the vulnerability
- Steps to reproduce it
- Potential impact
- Any suggested fixes if you have them

### What to Expect

- Acknowledgement within 48 hours
- Status update within 7 days
- Credit in the changelog if you would like it

---

## Security Design Decisions

This section documents the security choices made in this project so that
reviewers and contributors understand the reasoning behind them.

### Secrets Management
- All API keys and credentials are loaded exclusively from environment variables
- A `.env.example` file shows required variables without exposing real values
- `.env` is listed in `.gitignore` and must never be committed
- No credentials appear in code, comments, or logs under any circumstances
- GitLeaks or similar secret-scanning tools are recommended before every push

### Input Validation & Sanitization
- All user text input is validated for length and sanitized before being
  passed to the LLM to prevent prompt injection attacks
- File uploads are validated for type (allowlist only: PDF, TXT), size
  (configurable limit, default 10MB), and content before processing
- Filenames are sanitized and never used directly in file system operations

### Prompt Injection Protection
- User input is clearly separated from system instructions in every prompt
- System prompts are never exposed to end users
- Input containing patterns associated with prompt injection attempts
  (e.g. "ignore previous instructions") is flagged and handled safely

### Data Privacy
- Uploaded documents are processed in memory and never written to disk
  permanently
- No user questions, document contents, or personal data are ever logged
- Conversation history exists only in the Streamlit session state and is
  cleared when the session ends
- No analytics, tracking, or third-party data collection is used

### Dependency Security
- All dependencies are pinned to exact versions in `requirements.txt`
- Dependencies should be audited regularly using `pip audit`
- Minimal dependency footprint — only what is genuinely needed

### API Security
- Rate limiting is applied to both document upload and query operations
- The application never exposes raw API error messages to the user
  (errors are caught, logged safely, and a generic message is shown)

### Logging
- Structured logging records application events for debugging
- Logs never contain: API keys, user questions, document contents, or PII
- Log level is configurable via environment variable
