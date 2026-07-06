"""
schemas.py — Pydantic models for all API request/response shapes
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, HttpUrl, field_validator


class TestFramework(str, Enum):
    PLAYWRIGHT = "playwright"
    CYPRESS = "cypress"
    SELENIUM = "selenium"


class Language(str, Enum):
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    JAVA = "java"


# ── Requests ─────────────────────────────────

class GenerateRequest(BaseModel):
    repo_url: Optional[str] = None          # GitHub URL (mutually exclusive with zip)
    github_token: Optional[str] = None      # PAT for private repos
    framework: TestFramework = TestFramework.PLAYWRIGHT
    language: Language = Language.TYPESCRIPT
    test_flows: str = ""                     # user's description of flows to test
    base_url: str = "http://localhost:3000"
    include_ci: bool = True
    self_heal: bool = False                  # re-prompt LLM to fix invalid generated files

    @field_validator("repo_url")
    @classmethod
    def validate_github_url(cls, v):
        if v and "github.com" not in v:
            raise ValueError("Only GitHub URLs are supported.")
        return v


class StatusRequest(BaseModel):
    pass


# ── Auth ─────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class UserPublic(BaseModel):
    id: str
    email: Optional[str] = None
    name: Optional[str] = None
    github_login: Optional[str] = None
    avatar_url: Optional[str] = None
    has_github: bool = False


class AuthResponse(BaseModel):
    token: str
    user: UserPublic


# ── Responses ────────────────────────────────

class FilePreview(BaseModel):
    path: str
    size: int
    importance: int
    preview: str                            # first 200 chars


class ProjectAnalysis(BaseModel):
    project_summary: str
    framework: str
    key_pages: list[dict]
    key_components: list[dict]
    routes: list[str]
    testing_challenges: list[str]
    file_count: int
    total_tokens: int
    file_previews: list[FilePreview]


class GeneratedFile(BaseModel):
    filename: str
    content: str
    description: str
    size: int


class GenerateResponse(BaseModel):
    success: bool
    files: list[GeneratedFile]
    test_count: int
    framework: str
    selector_warnings: list[str]
    summary: str
    validation: list[dict] = []              # per-file syntax-validity results
    error: Optional[str] = None


class PublishResponse(BaseModel):
    success: bool
    repo_url: str
    full_name: str
    branch: str
    commit_sha: str
    files_pushed: int
    cicd_added: bool
    test_count: int = 0
    all_valid: bool = True                   # did every generated test file pass validation
    validation: list[dict] = []              # per-file syntax-validity results
    warnings: list[str] = []


class ScanRequest(BaseModel):
    url: str
    ai_summary: bool = True


class ScanResponse(BaseModel):
    url: str
    final_url: str
    score: int
    grade: str
    summary: str = ""
    counts: dict
    checks_run: int
    findings: list[dict] = []


class ProviderStatus(BaseModel):
    name: str
    total_keys: int
    available_keys: int
    total_calls: int
    healthy: bool


class StatusResponse(BaseModel):
    providers: list[ProviderStatus]
    call_log: list[dict]
