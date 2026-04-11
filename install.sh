#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# BinBrain Installer
# Handles: fresh install, upgrade, and dev mode
###############################################################################

# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/install.log"
ENV_FILE="${SCRIPT_DIR}/.env"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
COMPOSE_DEV_FILE="${SCRIPT_DIR}/docker-compose.dev.yml"
MIGRATIONS_DIR="${SCRIPT_DIR}/migrations"
BACKUPS_DIR="${SCRIPT_DIR}/backups"

DB_CONTAINER="binbrain_db"
API_CONTAINER="binbrain_api"
DB_USER="binbrain"
DB_NAME="binbrain"

DEFAULT_API_PORT=8000
DEFAULT_DB_PORT=5434

# ── Runtime state ────────────────────────────────────────────────────────────

MODE="install"
DEV_MODE=false
PULL_BASE=false
SKIP_BACKUP=false
API_PORT="${DEFAULT_API_PORT}"
DB_PORT="${DEFAULT_DB_PORT}"
OLLAMA_MISSING=false

# Staged env additions (for upgrade)
STAGED_ENV_LINES=()

# ── Color output and logging helpers ─────────────────────────────────────────

_has_color() { [[ -t 1 ]] && command -v tput >/dev/null 2>&1; }

if _has_color; then
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1)
    BLUE=$(tput setaf 4)
    BOLD=$(tput bold)
    RESET=$(tput sgr0)
else
    GREEN="" YELLOW="" RED="" BLUE="" BOLD="" RESET=""
fi

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}"
}

info() {
    printf '%s\n' "${GREEN}${BOLD}[OK]${RESET} $*"
    log "[INFO] $*"
}

warn() {
    printf '%s\n' "${YELLOW}${BOLD}[!!]${RESET} $*"
    log "[WARN] $*"
}

fail() {
    printf '%s\n' "${RED}${BOLD}[XX]${RESET} $*"
    log "[FAIL] $*"
}

# step() prints an "in progress" line; step_done() overwrites it with success
_STEP_MSG=""
step() {
    _STEP_MSG="$*"
    printf '%s' "${BLUE}${BOLD}[->]${RESET} ${_STEP_MSG}..."
    log "[STEP] ${_STEP_MSG}"
}

step_done() {
    local msg="${1:-${_STEP_MSG}}"
    printf '\r%s\n' "${GREEN}${BOLD}[OK]${RESET} ${msg}                    "
    log "[DONE] ${msg}"
    _STEP_MSG=""
}

die() {
    fail "$@"
    printf '    %s\n' "See ${LOG_FILE} for details."
    exit 1
}

# ── Argument parsing ─────────────────────────────────────────────────────────

usage() {
    printf '%s\n' \
        "Usage: install.sh [OPTIONS]" \
        "" \
        "Options:" \
        "  --upgrade        Run in upgrade mode (auto-detected if possible)" \
        "  --dev            Enable development mode (hot-reload)" \
        "  --api-port PORT  Set API port (default: ${DEFAULT_API_PORT})" \
        "  --db-port PORT   Set database port (default: ${DEFAULT_DB_PORT})" \
        "  --skip-backup    Skip pre-upgrade database backup" \
        "  --pull-base      Pull base images during build (docker compose build --pull)" \
        "  -h, --help       Show this help message"
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --upgrade)    MODE="upgrade"; shift ;;
            --dev)        DEV_MODE=true; shift ;;
            --api-port)
                [[ -z "${2:-}" ]] && die "--api-port requires a PORT argument"
                API_PORT="$2"; shift 2 ;;
            --db-port)
                [[ -z "${2:-}" ]] && die "--db-port requires a PORT argument"
                DB_PORT="$2"; shift 2 ;;
            --skip-backup) SKIP_BACKUP=true; shift ;;
            --pull-base)   PULL_BASE=true; shift ;;
            -h|--help)     usage ;;
            *)             die "Unknown option: $1 (try --help)" ;;
        esac
    done
}

# ── Port checking ────────────────────────────────────────────────────────────

check_port_available() {
    local port="$1"
    # Try lsof first (macOS + most Linux)
    if command -v lsof >/dev/null 2>&1; then
        if lsof -i :"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
            return 1
        fi
        return 0
    fi
    # Try ss (modern Linux)
    if command -v ss >/dev/null 2>&1; then
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            return 1
        fi
        return 0
    fi
    # Try netstat (legacy)
    if command -v netstat >/dev/null 2>&1; then
        if netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
            return 1
        fi
        return 0
    fi
    # No tool available -- warn and assume available
    warn "Cannot check port ${port} (no lsof, ss, or netstat). Proceeding anyway."
    return 0
}

# ── Auto-detect upgrade ─────────────────────────────────────────────────────

binbrain_containers_exist() {
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"
}

binbrain_container_owns_port() {
    local port="$1"
    docker ps --format '{{.Ports}}' --filter "name=${DB_CONTAINER}" --filter "name=${API_CONTAINER}" 2>/dev/null \
        | grep -q ":${port}->"
}

detect_mode() {
    if [[ "${MODE}" == "install" ]] && [[ -f "${ENV_FILE}" ]] && binbrain_containers_exist; then
        MODE="upgrade"
        info "Existing installation detected -- switching to upgrade mode"
    fi
}

# ── Prerequisites ────────────────────────────────────────────────────────────

check_prerequisites() {
    step "Checking prerequisites"
    local failures=0

    # Docker Engine
    if ! docker info >/dev/null 2>&1; then
        fail "Docker Engine is not running or not installed"
        log "docker info failed"
        failures=$((failures + 1))
    fi

    # Docker Compose v2
    if ! docker compose version >/dev/null 2>&1; then
        fail "Docker Compose v2 is required (docker compose, not docker-compose)"
        log "docker compose version failed"
        failures=$((failures + 1))
    fi

    # Ollama (optional)
    if ! command -v ollama >/dev/null 2>&1; then
        OLLAMA_MISSING=true
        warn "Ollama not found -- vision features will be unavailable"
        warn "Install from https://ollama.com and run: ollama pull qwen3-vl:2b"
    else
        # Check for vision model
        if ! ollama list 2>/dev/null | grep -q "qwen3-vl"; then
            OLLAMA_MISSING=true
            warn "Ollama vision model not found"
            warn "Run: ollama pull qwen3-vl:2b"
        fi
    fi

    # Port availability
    if [[ "${MODE}" == "upgrade" ]]; then
        # On upgrade, skip port check if BinBrain owns the port
        if ! check_port_available "${API_PORT}" && ! binbrain_container_owns_port "${API_PORT}"; then
            fail "Port ${API_PORT} is in use by another process"
            failures=$((failures + 1))
        fi
        if ! check_port_available "${DB_PORT}" && ! binbrain_container_owns_port "${DB_PORT}"; then
            fail "Port ${DB_PORT} is in use by another process"
            failures=$((failures + 1))
        fi
    else
        if ! check_port_available "${API_PORT}"; then
            fail "Port ${API_PORT} is already in use (try --api-port to pick another)"
            failures=$((failures + 1))
        fi
        if ! check_port_available "${DB_PORT}"; then
            fail "Port ${DB_PORT} is already in use (try --db-port to pick another)"
            failures=$((failures + 1))
        fi
    fi

    if [[ ${failures} -gt 0 ]]; then
        die "Prerequisites check failed (${failures} issue(s))"
    fi

    step_done "Prerequisites satisfied"
}

# ── .env generation ──────────────────────────────────────────────────────────

generate_random_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 16
    else
        dd if=/dev/urandom bs=512 count=1 2>/dev/null | tr -dc 'a-f0-9' | head -c 32
    fi
}

generate_env() {
    if [[ -f "${ENV_FILE}" ]]; then
        info ".env already exists -- skipping generation"
        return 0
    fi

    step "Generating .env"
    local password
    password="$(generate_random_password)"

    {
        printf '# BinBrain configuration -- generated %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf 'POSTGRES_PASSWORD=%s\n' "${password}"
        printf 'API_PORT=%s\n' "${API_PORT}"
        printf 'DB_PORT=%s\n' "${DB_PORT}"
        printf 'OLLAMA_URL=http://host.docker.internal:11434\n'
        printf 'OLLAMA_VISION_MODEL=qwen3-vl:2b\n'
        printf 'OLLAMA_MAX_IMAGE_PX=1280\n'
        printf 'YOLO_WORLD_CONF=0.15\n'
        if [[ "${DEV_MODE}" == true ]]; then
            printf 'BINBRAIN_ENV=development\n'
        fi
    } > "${ENV_FILE}"

    step_done "Generated .env"
    log "Wrote ${ENV_FILE}"
}

# For upgrades: detect missing env vars and stage additions
stage_env_additions() {
    local required_vars=(
        "POSTGRES_PASSWORD"
        "API_PORT"
        "DB_PORT"
        "OLLAMA_URL"
        "OLLAMA_VISION_MODEL"
        "OLLAMA_MAX_IMAGE_PX"
        "YOLO_WORLD_CONF"
    )
    local defaults=(
        ""
        "${API_PORT}"
        "${DB_PORT}"
        "http://host.docker.internal:11434"
        "qwen3-vl:2b"
        "1280"
        "0.15"
    )

    STAGED_ENV_LINES=()
    local i
    for i in "${!required_vars[@]}"; do
        local var="${required_vars[$i]}"
        if ! grep -q "^${var}=" "${ENV_FILE}" 2>/dev/null; then
            local val="${defaults[$i]}"
            if [[ -z "${val}" ]]; then
                warn "Missing ${var} in .env -- cannot auto-generate, skipping"
                continue
            fi
            STAGED_ENV_LINES+=("${var}=${val}")
            log "Staged new env var: ${var}"
        fi
    done

    if [[ ${#STAGED_ENV_LINES[@]} -gt 0 ]]; then
        info "Will add ${#STAGED_ENV_LINES[@]} new variable(s) to .env after migrations"
    fi
}

finalize_env() {
    if [[ ${#STAGED_ENV_LINES[@]} -eq 0 ]]; then
        return 0
    fi
    printf '\n# Added by upgrade on %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "${ENV_FILE}"
    local line
    for line in "${STAGED_ENV_LINES[@]}"; do
        printf '%s\n' "${line}" >> "${ENV_FILE}"
    done
    info "Appended ${#STAGED_ENV_LINES[@]} new variable(s) to .env"
}

# ── Docker build and start ───────────────────────────────────────────────────

build_images() {
    step "Building Docker images"
    local build_args=()
    if [[ "${PULL_BASE}" == true ]]; then
        build_args+=(--pull)
    fi
    docker compose -f "${COMPOSE_FILE}" build "${build_args[@]}" >> "${LOG_FILE}" 2>&1
    step_done "Docker images built"
}

start_services() {
    step "Starting services"
    local compose_args=(-f "${COMPOSE_FILE}")
    if [[ "${DEV_MODE}" == true ]]; then
        compose_args+=(-f "${COMPOSE_DEV_FILE}")
    fi
    docker compose "${compose_args[@]}" up -d >> "${LOG_FILE}" 2>&1
    step_done "Services started"
}

wait_for_db() {
    step "Waiting for database"
    local elapsed=0
    local timeout=60
    while [[ ${elapsed} -lt ${timeout} ]]; do
        if docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
            step_done "Database is ready"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    die "Database did not become ready within ${timeout}s"
}

# ── Health check ─────────────────────────────────────────────────────────────

health_check() {
    step "Running API health check"
    local elapsed=0
    local timeout=30
    local backoff=2
    while [[ ${elapsed} -lt ${timeout} ]]; do
        if docker exec "${API_CONTAINER}" python -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://localhost:8000/health', timeout=5)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
            step_done "API is healthy"
            return 0
        fi
        sleep "${backoff}"
        elapsed=$((elapsed + backoff))
        # Increase backoff: 2, 3, 4, 5 ...
        if [[ ${backoff} -lt 5 ]]; then
            backoff=$((backoff + 1))
        fi
    done
    die "API health check failed after ${timeout}s"
}

# ── SQL helpers ──────────────────────────────────────────────────────────────

db_exec() {
    docker exec -i "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" "$@"
}

db_exec_quiet() {
    docker exec -i "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -tAc "$@"
}

db_exec_file() {
    local file="$1"
    docker exec -i "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" < "${file}"
}

# ── Migration functions ──────────────────────────────────────────────────────

ensure_migrations_table() {
    db_exec_quiet "CREATE TABLE IF NOT EXISTS schema_migrations (
        filename text PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT now()
    );" >> "${LOG_FILE}" 2>&1
}

# Fresh install: schema already applied by 01-schema.sql, just record all migrations
mark_all_migrations_applied() {
    step "Recording migrations as applied"
    ensure_migrations_table

    local migration
    for migration in "${MIGRATIONS_DIR}"/*.sql; do
        [[ -f "${migration}" ]] || continue
        local filename
        filename="$(basename "${migration}")"
        db_exec_quiet "INSERT INTO schema_migrations (filename) VALUES ('${filename}') ON CONFLICT DO NOTHING;" \
            >> "${LOG_FILE}" 2>&1
    done
    step_done "All migrations recorded"
}

# Upgrade: run only unapplied migrations
run_migrations() {
    step "Running database migrations"
    ensure_migrations_table

    local applied=0
    local skipped=0
    local migration

    for migration in "${MIGRATIONS_DIR}"/*.sql; do
        [[ -f "${migration}" ]] || continue
        local filename
        filename="$(basename "${migration}")"

        # Check if already applied
        local exists
        exists="$(db_exec_quiet "SELECT 1 FROM schema_migrations WHERE filename = '${filename}';" 2>/dev/null || true)"
        if [[ "${exists}" == "1" ]]; then
            skipped=$((skipped + 1))
            continue
        fi

        log "Applying migration: ${filename}"

        # Detect CONCURRENTLY keyword -- cannot run inside a transaction
        if grep -qi 'CONCURRENTLY' "${migration}"; then
            log "Migration ${filename} uses CONCURRENTLY -- running outside transaction"
            db_exec_file "${migration}" >> "${LOG_FILE}" 2>&1
            db_exec_quiet "INSERT INTO schema_migrations (filename) VALUES ('${filename}');" \
                >> "${LOG_FILE}" 2>&1
        else
            # Run in a transaction: migration SQL + record in schema_migrations
            {
                printf 'BEGIN;\n'
                sed 's/$//' "${migration}"
                printf '\n'
                printf "INSERT INTO schema_migrations (filename) VALUES ('%s');\n" "${filename}"
                printf 'COMMIT;\n'
            } | db_exec >> "${LOG_FILE}" 2>&1
        fi

        applied=$((applied + 1))
    done

    step_done "Migrations complete (${applied} applied, ${skipped} already up-to-date)"
}

# ── Backup ───────────────────────────────────────────────────────────────────

backup_database() {
    if [[ "${SKIP_BACKUP}" == true ]]; then
        warn "Skipping pre-upgrade backup (--skip-backup)"
        return 0
    fi

    step "Backing up database"
    mkdir -p "${BACKUPS_DIR}"
    local timestamp
    timestamp="$(date '+%Y%m%d_%H%M%S')"
    local backup_file="${BACKUPS_DIR}/binbrain-pre-upgrade-${timestamp}.sql"

    docker exec "${DB_CONTAINER}" pg_dump -U "${DB_USER}" "${DB_NAME}" > "${backup_file}" 2>> "${LOG_FILE}"
    step_done "Database backed up to ${backup_file}"
}

# ── Cleanup and summary ─────────────────────────────────────────────────────

cleanup_on_failure() {
    if [[ "${MODE}" == "install" ]]; then
        warn "Installation failed -- stopping containers"
        docker compose -f "${COMPOSE_FILE}" down >> "${LOG_FILE}" 2>&1 || true
    fi
}

print_summary() {
    printf '\n'
    printf '%s\n' "${GREEN}${BOLD}============================================${RESET}"
    printf '%s\n' "${GREEN}${BOLD}  BinBrain ${MODE} complete${RESET}"
    printf '%s\n' "${GREEN}${BOLD}============================================${RESET}"
    printf '\n'
    printf '  API:   %s\n' "http://localhost:${API_PORT}"
    printf '  Docs:  %s\n' "http://localhost:${API_PORT}/docs"
    if [[ "${DEV_MODE}" == true ]]; then
        printf '  Mode:  %s\n' "${YELLOW}development (hot-reload)${RESET}"
    fi
    if [[ "${OLLAMA_MISSING}" == true ]]; then
        printf '\n'
        warn "Ollama/vision model not detected -- vision features disabled"
        printf '  Install Ollama:  %s\n' "https://ollama.com"
        printf '  Pull model:      %s\n' "ollama pull qwen3-vl:2b"
    fi
    printf '\n'
    printf '  Next steps:\n'
    printf '    1. Create an API key:\n'
    printf '       curl -X POST http://localhost:%s/api-keys -H "Content-Type: application/json" \\\n' "${API_PORT}"
    printf '            -d '"'"'{"name": "my-key"}'"'"'\n'
    printf '    2. Read the docs: http://localhost:%s/docs\n' "${API_PORT}"
    printf '\n'
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    # Initialize log
    printf '=== BinBrain installer started %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" > "${LOG_FILE}"

    parse_args "$@"
    detect_mode
    log "Mode: ${MODE}, Dev: ${DEV_MODE}, API port: ${API_PORT}, DB port: ${DB_PORT}"

    trap cleanup_on_failure ERR

    if [[ "${MODE}" == "install" ]]; then
        info "Starting fresh installation"
        check_prerequisites
        generate_env
        build_images
        start_services
        wait_for_db
        mark_all_migrations_applied
        health_check
    else
        info "Starting upgrade"
        check_prerequisites
        stage_env_additions
        backup_database
        build_images
        # Ensure DB is ready before migrating (may be stopped)
        wait_for_db
        # Run migrations against the still-running DB before restarting
        run_migrations
        start_services
        wait_for_db
        health_check
        finalize_env
    fi

    trap - ERR
    print_summary
}

main "$@"
