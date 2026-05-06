"""Import CIAA cases from JSON files in R2/S3 bucket as draft cases."""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from cloudpathlib import AnyPath, S3Client
except ImportError as e:
    raise ImportError(
        "cloudpathlib is required for this command. " "poetry add jawafdehi-api[s3]"
    ) from e

from django.core.management.base import BaseCommand, CommandError

from cases.services.ciaa_draft_case_service import CIAADraftCaseService

# Configure console logging (concise)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(console_handler)

# Default base path for CIAA dataset (can be overridden via env var or --base-path)
# Public HTTPS URL: https://ngm-store.jawafdehi.org/uploads/ciaa/cases
# S3 path (requires credentials): s3://ngm/uploads/ciaa/cases
DEFAULT_BASE_PATH = os.getenv(
    "CIAA_DATASET_BASE_PATH", "https://ngm-store.jawafdehi.org/uploads/ciaa/cases"
)


class Command(BaseCommand):
    help = "Import CIAA cases from JSON files produced by NGM service"

    def __init__(self):
        super().__init__()
        self.file_logger = None
        self.log_file_path = None
        self.import_start_time = None

    def add_arguments(self, parser):
        """Define command-line arguments."""
        parser.add_argument(
            "--fiscal-year",
            type=str,
            help="Fiscal year (e.g., '2078-79'). If not provided, imports all available years",
        )
        parser.add_argument(
            "--base-path",
            type=str,
            default=None,
            help=f"Base path (default: {DEFAULT_BASE_PATH})",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Validate without saving"
        )

    def _setup_file_logging(self, log_dir: str, dry_run: bool):
        """Set up detailed file logging for rollback purposes."""
        self.import_start_time = datetime.now()
        timestamp = self.import_start_time.strftime("%Y%m%d_%H%M%S")
        mode = "dryrun" if dry_run else "import"

        # Create log directory
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # Create log file
        self.log_file_path = log_path / f"{mode}_{timestamp}.log"

        # Set up file handler with detailed formatting
        file_handler = logging.FileHandler(self.log_file_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

        # Add handler to command logger
        self.file_logger = logging.getLogger(f"{__name__}.file")
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.addHandler(file_handler)
        self.file_logger.propagate = False

        # Also add handler to service logger to capture entity/source logs
        service_logger = logging.getLogger("cases.services.ciaa_draft_case_service")
        service_logger.addHandler(file_handler)
        service_logger.setLevel(logging.INFO)

        # Log header
        self.file_logger.info("=" * 80)
        self.file_logger.info(
            f"CIAA CASE IMPORT LOG - {'DRY RUN' if dry_run else 'PRODUCTION'}"
        )
        self.file_logger.info("=" * 80)
        self.file_logger.info(f"Start Time: {self.import_start_time.isoformat()}")
        self.file_logger.info(f"Log File: {self.log_file_path}")
        self.file_logger.info("")

    def _log_to_file(self, level: str, message: str, **kwargs):
        """Log detailed information to file for rollback purposes."""
        if not self.file_logger:
            return

        log_method = getattr(self.file_logger, level.lower(), self.file_logger.info)

        # Format message with additional context
        if kwargs:
            # For case-related logs, use multi-line format for readability
            if message in [
                "CASE CREATED",
                "CASE SKIPPED - Already Exists",
                "CASE SKIPPED - Not Confirmed",
                "CASE FAILED",
            ]:
                lines = [message]
                for k, v in kwargs.items():
                    lines.append(f"  {k}: {v}")
                full_message = "\n".join(lines)
            else:
                # For other logs, use compact single-line format
                context_str = " | ".join(f"{k}={v}" for k, v in kwargs.items())
                full_message = f"{message} | {context_str}"
        else:
            full_message = message

        log_method(full_message)

    def handle(self, *args, **options):
        """Execute the import command."""
        fiscal_year = options.get("fiscal_year")
        base_path = options.get("base_path") or os.getenv(
            "CIAA_DATASET_BASE_PATH", DEFAULT_BASE_PATH
        )
        dry_run = options["dry_run"]

        # Set up detailed file logging
        self._setup_file_logging("logs/import_ciaa_cases", dry_run)

        # Log command parameters
        self._log_to_file(
            "info",
            "Command Parameters",
            fiscal_year=fiscal_year or "ALL",
            base_path=base_path,
            dry_run=dry_run,
        )

        # Configure S3Client only if using S3 paths (s3://)
        # For HTTPS paths, cloudpathlib will use HTTP client automatically
        if base_path.startswith("s3://"):
            endpoint_url = os.getenv("AWS_ENDPOINT_URL") or os.getenv(
                "AWS_S3_ENDPOINT_URL"
            )
            access_key = os.getenv("AWS_ACCESS_KEY_ID")
            secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")

            if access_key and secret_key:
                # Use credentials for S3 API access
                S3Client(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    endpoint_url=endpoint_url,
                ).set_as_default_client()
                logger.info("Configured S3Client with credentials for R2 storage")
                self._log_to_file(
                    "info",
                    "S3 Configuration",
                    auth="credentials",
                    endpoint=endpoint_url,
                )
            else:
                # Try anonymous access (may not work for all buckets)
                S3Client(
                    endpoint_url=endpoint_url,
                    no_sign_request=True,
                ).set_as_default_client()
                logger.info("Configured S3Client for anonymous R2 access")
                self._log_to_file(
                    "info",
                    "S3 Configuration",
                    auth="anonymous",
                    endpoint=endpoint_url,
                    note="May fail if bucket requires authentication",
                )
        else:
            # Using HTTPS URL - no S3 configuration needed
            logger.info("Using HTTPS access (no S3 client needed)")
            self._log_to_file(
                "info",
                "Access Method",
                method="HTTPS",
                note="Public bucket access via HTTP client",
            )

        # Mask sensitive path info - only show bucket name
        bucket_name = (
            base_path.split("/")[2] if base_path.startswith("s3://") else "local"
        )
        logger.info(f"Starting import from bucket: {bucket_name}")
        self._log_to_file("info", "Import Started", bucket=bucket_name, path=base_path)

        if dry_run:
            logger.warning("DRY-RUN MODE: No changes will be saved")
            self._log_to_file("warning", "DRY-RUN MODE ENABLED")

        try:
            base_path_obj = AnyPath(base_path)

            # If no fiscal year specified, discover all available fiscal years
            if not fiscal_year:
                fiscal_years = self._discover_fiscal_years(base_path_obj)
                if not fiscal_years:
                    logger.warning("No fiscal year directories found")
                    self._log_to_file("warning", "No fiscal years found")
                    return
                logger.info(
                    f"Found {len(fiscal_years)} fiscal years: {', '.join(fiscal_years)}"
                )
                self._log_to_file(
                    "info",
                    "Fiscal Years Discovered",
                    count=len(fiscal_years),
                    years=", ".join(fiscal_years),
                )
            else:
                fiscal_years = [fiscal_year]
                self._log_to_file("info", "Single Fiscal Year Mode", year=fiscal_year)

            total_created = total_skipped = total_failed = 0

            # Track cumulative entity and source statistics
            cumulative_stats = {
                "entities_created": 0,
                "entities_reused": 0,
                "sources_created": 0,
                "sources_reused": 0,
            }

            for fy in fiscal_years:
                logger.info(f"\n{'='*60}")
                logger.info(f"Processing fiscal year: {fy}")
                logger.info(f"{'='*60}")
                self._log_to_file("info", f"{'='*80}")
                self._log_to_file("info", f"PROCESSING FISCAL YEAR: {fy}")
                self._log_to_file("info", f"{'='*80}")

                created, skipped, failed, fy_stats = self._import_fiscal_year(
                    base_path_obj, fy, dry_run
                )

                total_created += created
                total_skipped += skipped
                total_failed += failed

                # Accumulate stats
                for key in cumulative_stats:
                    cumulative_stats[key] += fy_stats.get(key, 0)

                self._log_to_file(
                    "info",
                    f"Fiscal Year {fy} Complete",
                    created=created,
                    skipped=skipped,
                    failed=failed,
                )

            self._log_summary(
                total_created, total_skipped, total_failed, cumulative_stats, dry_run
            )

            # Log completion
            import_end_time = datetime.now()
            duration = (import_end_time - self.import_start_time).total_seconds()
            self._log_to_file("info", "=" * 80)
            self._log_to_file(
                "info",
                "IMPORT COMPLETED",
                end_time=import_end_time.isoformat(),
                duration_seconds=f"{duration:.2f}",
                total_created=total_created,
                total_skipped=total_skipped,
                total_failed=total_failed,
            )
            self._log_to_file("info", "=" * 80)

            logger.info(f"\nDetailed log saved to: {self.log_file_path}")

            # Exit with error code if any imports failed
            if total_failed > 0:
                raise CommandError(f"Import completed with {total_failed} failures")

        except CommandError:
            self._log_to_file("error", "Command Error Raised")
            raise
        except Exception as e:
            self._log_to_file("error", f"Unexpected Error: {e}", exception=str(e))
            raise CommandError(f"Import failed: {e}") from e

    def _discover_fiscal_years(self, base_path: AnyPath) -> list[str]:
        """Discover all fiscal year directories in base path."""
        self._log_to_file("info", "Discovering fiscal years", base_path=str(base_path))
        fiscal_years = []
        try:
            for item in base_path.iterdir():
                if item.is_dir() and "-" in item.name:
                    fiscal_years.append(item.name)
                    self._log_to_file(
                        "debug", "Found fiscal year directory", year=item.name
                    )
            sorted_years = sorted(fiscal_years)
            self._log_to_file(
                "info",
                "Fiscal year discovery complete",
                count=len(sorted_years),
                years=", ".join(sorted_years),
            )
            return sorted_years
        except FileNotFoundError as e:
            self._log_to_file(
                "error", "Base path not found", path=str(base_path), error=str(e)
            )
            raise CommandError(f"Base path not found: {base_path}") from e
        except PermissionError as e:
            self._log_to_file(
                "error", "Permission denied", path=str(base_path), error=str(e)
            )
            raise CommandError(f"Permission denied accessing {base_path}") from e
        except Exception as e:
            self._log_to_file("error", "Failed to discover fiscal years", error=str(e))
            raise CommandError(f"Failed to discover fiscal years: {e}") from e

    def _import_fiscal_year(
        self, base_path: AnyPath, fiscal_year: str, dry_run: bool
    ) -> tuple[int, int, int, dict]:
        """Import cases for a single fiscal year. Returns (created, skipped, failed, stats)."""
        source_dir = base_path / fiscal_year

        self._log_to_file(
            "info",
            "Starting fiscal year import",
            fiscal_year=fiscal_year,
            source_dir=str(source_dir),
        )

        # Validate fiscal year directory exists
        if not source_dir.exists():
            self._log_to_file(
                "error",
                "Fiscal year directory not found",
                fiscal_year=fiscal_year,
                path=str(source_dir),
            )
            raise CommandError(
                f"Fiscal year directory not found: {fiscal_year}\n"
                f"Check that the directory exists in the base path."
            )

        json_files = list(source_dir.rglob("*.json"))
        json_files = [f for f in json_files if f.name != "index.json"]

        self._log_to_file(
            "info",
            "JSON files discovered",
            fiscal_year=fiscal_year,
            total_files=len(json_files),
        )

        if not json_files:
            logger.warning(f"No JSON files found in {fiscal_year}")
            self._log_to_file("warning", "No JSON files found", fiscal_year=fiscal_year)
            return 0, 0, 0, {}

        logger.info(f"Found {len(json_files)} JSON files")

        service = CIAADraftCaseService()
        created = skipped = failed = 0
        skipped_not_confirmed = 0

        for idx, json_file in enumerate(json_files, 1):
            file_name = json_file.name
            try:
                ciaa_json = json.loads(json_file.read_text(encoding="utf-8"))

                self._log_to_file(
                    "debug",
                    f"Processing file {idx}/{len(json_files)}",
                    file=file_name,
                    case_no=ciaa_json.get("case_no", "Unknown"),
                )

                if ciaa_json.get("meta", {}).get("match_status") != "confirmed":
                    skipped_not_confirmed += 1
                    match_status = ciaa_json.get("meta", {}).get(
                        "match_status", "unknown"
                    )
                    case_no = ciaa_json.get("case_no", "Unknown")
                    case_title = ciaa_json.get("case_title", "")

                    self._log_to_file(
                        "info",
                        "CASE SKIPPED - Not Confirmed",
                        file=file_name,
                        case_no=case_no,
                        case_title=case_title,
                        match_status=match_status,
                        reason=f"match_status is '{match_status}', not 'confirmed'",
                    )
                    continue

                case_no = ciaa_json.get("case_no", "Unknown")
                case_title = ciaa_json.get("case_title", "")[:60]

                logger.info(
                    f"[{idx}/{len(json_files)}] Processing: {case_no} - {case_title}..."
                )

                result = service.import_case(ciaa_json, dry_run=dry_run)

                if result.status == "created":
                    created += 1
                    # Get stats from service
                    entities_count = len(
                        ciaa_json.get("court_case", {}).get("defendants", [])
                    )
                    sources_count = len(
                        ciaa_json.get("ciaa", {}).get("press_releases", [])
                    ) + len(ciaa_json.get("court_case", {}).get("faisala_link", []))

                    logger.info(
                        f"DRAFTED: {case_no} | "
                        f"{entities_count} defendant(s), {sources_count} source(s)"
                    )

                    self._log_to_file(
                        "info",
                        "CASE CREATED",
                        case_id=result.case_id,
                        case_no=case_no,
                        case_title=ciaa_json.get("case_title", ""),
                        file=file_name,
                        entities_count=entities_count,
                        sources_count=sources_count,
                        court_cases=ciaa_json.get("court_case", {}).get("case_no"),
                        registration_date=ciaa_json.get("court_case", {}).get(
                            "registration_date_ad"
                        ),
                    )

                elif result.status == "skipped":
                    skipped += 1
                    logger.info(f"SKIPPED: {case_no} (already exists)")

                    self._log_to_file(
                        "info",
                        "CASE SKIPPED - Already Exists",
                        case_id=result.case_id,
                        case_no=case_no,
                        file=file_name,
                        reason=result.message,
                    )
                else:
                    failed += 1
                    logger.error(f"FAILED: {case_no} - {result.message}")

                    self._log_to_file(
                        "error",
                        "CASE FAILED",
                        case_no=case_no,
                        file=file_name,
                        error=result.message,
                        errors="; ".join(result.errors) if result.errors else "",
                    )

            except json.JSONDecodeError as e:
                failed += 1
                logger.error(f"JSON parse error in {file_name}: {e}")
                self._log_to_file(
                    "error",
                    "JSON PARSE ERROR",
                    file=file_name,
                    error=str(e),
                    line=e.lineno if hasattr(e, "lineno") else None,
                )
            except Exception as e:
                failed += 1
                logger.error(f"Error processing {file_name}: {e}")
                self._log_to_file(
                    "error",
                    "PROCESSING ERROR",
                    file=file_name,
                    error=str(e),
                    exception_type=type(e).__name__,
                )

        if skipped_not_confirmed > 0:
            logger.info(
                f"Skipped {skipped_not_confirmed} cases (not confirmed match_status)"
            )
            self._log_to_file(
                "info",
                "Skipped unconfirmed cases",
                count=skipped_not_confirmed,
                reason="match_status != 'confirmed'",
            )

        # Log entity and source statistics for this fiscal year
        self._log_to_file(
            "info",
            "Entity Statistics",
            fiscal_year=fiscal_year,
            entities_created=service.stats["entities_created"],
            entities_reused=service.stats["entities_reused"],
            total_entities=service.stats["entities_created"]
            + service.stats["entities_reused"],
        )

        self._log_to_file(
            "info",
            "Source Statistics",
            fiscal_year=fiscal_year,
            sources_created=service.stats["sources_created"],
            sources_reused=service.stats["sources_reused"],
            total_sources=service.stats["sources_created"]
            + service.stats["sources_reused"],
        )

        return created, skipped, failed, service.stats

    def _log_summary(self, created, skipped, failed, stats, dry_run):
        """Log detailed import summary with statistics."""
        logger.info("\n" + "=" * 60)
        logger.info("IMPORT SUMMARY")
        logger.info("=" * 60)
        if dry_run:
            logger.warning("DRY-RUN MODE (no changes saved)")

        total = created + skipped + failed
        logger.info(f"Total processed: {total}")
        logger.info(
            f"Created:       {created} ({created/total*100:.1f}%)"
            if total > 0
            else f"Created:       {created}"
        )
        logger.info(
            f"Skipped:       {skipped} ({skipped/total*100:.1f}%)"
            if total > 0
            else f"Skipped:       {skipped}"
        )
        logger.info(
            f"Failed:        {failed} ({failed/total*100:.1f}%)"
            if total > 0
            else f"Failed:        {failed}"
        )
        logger.info("=" * 60)

        if created > 0:
            logger.info(f"Successfully drafted {created} new case(s)")
        if skipped > 0:
            logger.info(f"Skipped {skipped} existing case(s)")
        if failed > 0:
            logger.error(f"{failed} case(s) failed to import")

        # Log entity and source statistics to console
        total_entities = stats["entities_created"] + stats["entities_reused"]
        total_sources = stats["sources_created"] + stats["sources_reused"]

        if total_entities > 0:
            logger.info(
                f"Entities: {stats['entities_created']} new, "
                f"{stats['entities_reused']} reused ({total_entities} total)"
            )
        if total_sources > 0:
            logger.info(
                f"Sources: {stats['sources_created']} new, "
                f"{stats['sources_reused']} reused ({total_sources} total)"
            )

        # Log to file with more detail
        self._log_to_file("info", "=" * 80)
        self._log_to_file("info", "IMPORT SUMMARY")
        self._log_to_file("info", "=" * 80)
        self._log_to_file(
            "info",
            "Case Statistics",
            total_processed=total,
            created=created,
            created_pct=f"{created/total*100:.1f}%" if total > 0 else "0%",
            skipped=skipped,
            skipped_pct=f"{skipped/total*100:.1f}%" if total > 0 else "0%",
            failed=failed,
            failed_pct=f"{failed/total*100:.1f}%" if total > 0 else "0%",
            dry_run=dry_run,
        )

        self._log_to_file(
            "info",
            "Entity Statistics (Cumulative)",
            entities_created=stats["entities_created"],
            entities_reused=stats["entities_reused"],
            total_entities=total_entities,
        )

        self._log_to_file(
            "info",
            "Source Statistics (Cumulative)",
            sources_created=stats["sources_created"],
            sources_reused=stats["sources_reused"],
            total_sources=total_sources,
        )
