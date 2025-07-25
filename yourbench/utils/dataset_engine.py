import os
import math
import random
import shutil
import tempfile
from typing import Any, Set, List, TypeVar, Sequence
from pathlib import Path
from contextlib import suppress
from dataclasses import dataclass

from loguru import logger

from datasets import Dataset, DatasetDict, load_dataset, load_from_disk, concatenate_datasets
from huggingface_hub import HfApi, DatasetCard, DatasetCardData, whoami
from huggingface_hub.utils import HFValidationError


__all__ = [
    "custom_load_dataset",
    "custom_save_dataset",
    "upload_dataset_card",
    "get_hf_settings",
    "replace_dataset_columns",
]

T = TypeVar("T")


class ConfigurationError(Exception):
    """Configuration error."""


@dataclass(slots=True, frozen=True)
class HFSettings:
    """Normalized HuggingFace configuration."""

    dataset_name: str
    organization: str | None
    token: str | None
    local_dir: Path | None
    local_saving: bool = True
    concat_if_exist: bool = False
    private: bool = True

    @property
    def repo_id(self) -> str:
        """Full repository identifier."""
        if "/" in self.dataset_name:
            return self.dataset_name
        return f"{self.organization}/{self.dataset_name}" if self.organization else self.dataset_name


def _is_offline() -> bool:
    """Check if offline mode enabled."""
    return os.environ.get("HF_HUB_OFFLINE", "0").lower() in ("1", "true", "yes")


def _expand_var(value: str, field: str) -> str:
    """Ensure value is not unexpanded $VAR placeholder."""
    if value.startswith("$"):
        var_name = value[1:].split("/")[0]
        msg = f"Environment variable '{var_name}' in '{field}' not set"
        logger.error(msg)
        raise ConfigurationError(msg)
    return value


def get_hf_settings(config: dict[str, Any]) -> HFSettings:
    """Public getter for HF settings used in other modules."""
    return _extract_settings(config)


def _extract_settings(config: dict[str, Any]) -> HFSettings:
    """Parse and validate configuration."""
    if "hf_configuration" not in config:
        raise ConfigurationError("'hf_configuration' section missing")

    hf = config["hf_configuration"]
    if "hf_dataset_name" not in hf:
        raise ConfigurationError("'hf_dataset_name' required")

    dataset_name = _expand_var(hf["hf_dataset_name"], "hf_dataset_name")
    org_raw = hf.get("hf_organization")
    token = hf.get("token") or os.getenv("HF_TOKEN")

    organization = _resolve_organization(org_raw, token)

    local_raw = config.get("local_dataset_dir") or hf.get("local_dataset_dir")
    local_dir = Path(local_raw).expanduser().resolve() if local_raw else None

    return HFSettings(
        dataset_name=dataset_name,
        organization=organization,
        token=token,
        local_dir=local_dir,
        local_saving=hf.get("local_saving", False),
        concat_if_exist=hf.get("concat_if_exist", False),
        private=hf.get("private", True),
    )


def _resolve_organization(org: str | None, token: str | None) -> str | None:
    """Resolve organization, fetching from HF if needed."""
    if _is_offline() or (org and not org.startswith("$")):
        return org

    if org and org.startswith("$"):
        var_name = org[1:].split("/")[0]
        logger.warning(f"Environment variable '{var_name}' in 'hf_organization' not set")

    if not token:
        return None

    try:
        if username := whoami(token=token).get("name"):
            logger.info(f"Using '{username}' as organization")
            return username
    except HFValidationError:
        logger.warning("Invalid HF token")
    except (ConnectionError, TimeoutError) as e:
        logger.warning(f"Network error fetching organization: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching organization: {e}")

    return None


def _validate_repo(settings: HFSettings) -> None:
    """Validate repository ID format."""
    if _is_offline():
        return

    try:
        HfApi().repo_info(repo_id=settings.repo_id, repo_type="dataset", token=settings.token)
    except HFValidationError as e:
        raise ConfigurationError(f"Invalid repo ID '{settings.repo_id}': {e}") from e
    except (ConnectionError, TimeoutError) as e:
        logger.warning(f"Network error validating repo: {e}")
    except Exception as e:
        if "404" not in str(e):
            logger.error(f"Unexpected error validating repo: {e}")
            raise


def _load_local(path: Path, subset: str | None) -> Dataset:
    """Load dataset from local path with detailed inspection logs."""
    logger.info(f"Loading '{subset or 'default'}' from {path}")
    dataset = load_from_disk(str(path))

    logger.debug(f"Loaded type: {type(dataset)}")

    logger.debug(f"Directory contents: {list(path.iterdir())}")

    if subset is None or not isinstance(dataset, DatasetDict):
        return dataset

    if subset in dataset:
        return dataset[subset]

    raise ConfigurationError(f"Subset '{subset}' not found in local dataset")


def _load_hub(repo_id: str, subset: str | None, token: str | None) -> Dataset:
    """Load dataset from HuggingFace Hub."""
    logger.info(f"Loading '{subset or 'default'}' from Hub: {repo_id}")

    try:
        dataset = load_dataset(repo_id, name=subset, split="train", token=token)
        if len(dataset) == 0:
            raise ValueError(f"Dataset from Hub is empty (repo: {repo_id}, subset: {subset})")
        return dataset
    except ValueError as e:
        if "BuilderConfig" in str(e) and "not found" in str(e):
            raise ConfigurationError(f"Subset '{subset}' not found on Hub") from e
        if "split" in str(e):
            raise ConfigurationError("Split 'train' not found in dataset") from e
        raise


def _merge_datasets(existing: Dataset | DatasetDict, new: Dataset, subset: str | None) -> Dataset | DatasetDict:
    """Merge new dataset with existing. If subset exists, new data is concatenated."""
    if subset is None:
        if isinstance(existing, Dataset):
            return concatenate_datasets([existing, new])
        return new

    if not isinstance(existing, DatasetDict):
        existing = DatasetDict({"default": existing})

    if subset in existing:
        try:
            # Concatenate new data with the existing subset
            new = concatenate_datasets([existing[subset], new])
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(
                f"Could not concatenate for subset '{subset}' (e.g., schema mismatch). Overwriting. Error: {e}"
            )

    existing[subset] = new
    return existing


def _safe_save(dataset: Dataset | DatasetDict, path: Path) -> None:
    """Save dataset, handling overwrite issues."""
    try:
        dataset.save_to_disk(str(path))
        logger.success(f"Saved to {path}")
    except PermissionError as e:
        if "can't overwrite itself" not in str(e):
            raise

        with tempfile.TemporaryDirectory() as tmp:
            dataset.save_to_disk(tmp)
            shutil.rmtree(path, ignore_errors=True)
            shutil.copytree(tmp, path)
        logger.success(f"Saved to {path} (via temp)")


def custom_load_dataset(config: dict[str, Any], subset: str | None = None) -> Dataset:
    """Load dataset subset from local path or Hub. Raises errors if data missing or invalid."""
    settings = _extract_settings(config)

    if settings.local_dir and settings.local_dir.exists():
        return _load_local(settings.local_dir, subset)

    if _is_offline():
        raise RuntimeError("Offline mode enabled but no local dataset found")

    _validate_repo(settings)
    return _load_hub(settings.repo_id, subset, settings.token)


def custom_save_dataset(
    dataset: Dataset,
    config: dict[str, Any],
    subset: str | None = None,
    *,
    save_local: bool = True,
    push_to_hub: bool = True,
) -> None:
    """Save dataset locally and/or push to Hub."""
    settings = _extract_settings(config)

    if _is_offline():
        save_local = True
        push_to_hub = False
        logger.info("Offline mode - only saving locally")

    # Check both local_saving flag and local_dir existence
    if save_local and settings.local_saving and settings.local_dir:
        logger.info(f"Saving to {settings.local_dir}")

        existing = None
        if settings.local_dir.exists():
            logger.info(f"Loading existing dataset at: {settings.local_dir}")
            try:
                existing = load_from_disk(str(settings.local_dir))
            except (FileNotFoundError, PermissionError, OSError) as e:
                logger.warning(f"Error loading existing dataset from disk: {e}")
            except Exception as e:
                logger.error(f"Unexpected error loading existing dataset: {e}")
                raise

        if settings.concat_if_exist and existing:
            new = concatenate_datasets([existing, dataset])
        elif existing and subset:
            # only add subset to existing dataframe
            existing[subset] = dataset
            new = existing
        else:
            new = DatasetDict({subset: dataset}) if subset else dataset

        # Ensure the local directory exists before saving
        settings.local_dir.mkdir(parents=True, exist_ok=True)
        _safe_save(new, settings.local_dir)
    elif save_local and settings.local_saving and not settings.local_dir:
        logger.warning("Local saving enabled but no local_dataset_dir specified in configuration")
    elif save_local and not settings.local_saving:
        logger.debug("Local saving skipped (local_saving=False in configuration)")

    # TODO also update this part on how concat and merge is done
    if push_to_hub and not _is_offline():
        if settings.concat_if_exist:
            with suppress(Exception):
                existing = _load_hub(settings.repo_id, subset, settings.token)
                dataset = concatenate_datasets([existing, dataset])
                logger.info("Concatenated with existing remote")

        _validate_repo(settings)
        logger.info(f"Pushing to Hub: {settings.repo_id}")
        dataset.push_to_hub(
            repo_id=settings.repo_id,
            private=settings.private,
            config_name=subset or "default",
            token=settings.token,
        )
        logger.success(f"Pushed to Hub: {settings.repo_id}")


def replace_dataset_columns(
    dataset: Dataset, columns_data: dict[str, list], preserve_metadata: bool = False
) -> Dataset:
    """Replace columns by removing existing and adding new ones."""
    to_remove = [col for col in columns_data if col in dataset.column_names]

    if to_remove:
        logger.info(f"Removing columns: {to_remove}")
        dataset = dataset.remove_columns(to_remove)

    for name, data in columns_data.items():
        dataset = dataset.add_column(name, data)

    return dataset


def _unrank_comb(n: int, k: int, rank: int) -> List[int]:
    """
    Return the k-combination of [0, n) corresponding to the given rank
    in colexicographic (colex) order.

    Colexicographic order sorts combinations by increasing values of the
    largest element, then second largest, and so on (i.e., right-to-left
    significance).

    Parameters
    ----------
    n : int
        Size of the universe (exclusive upper bound of elements).
    k : int
        Size of each combination.
    rank : int
        Integer in the range [0, C(n, k)) specifying the position of the combination
        in colexicographic order.

    Returns
    -------
    List[int]
        A strictly increasing list of k integers in the range [0, n),
        representing the rank-th combination in colex order.

    Raises
    ------
    ValueError
        If k is not in [0, n] or rank is not in [0, C(n, k)).
    """
    if not 0 <= k <= n:
        raise ValueError(f"require 0 ≤ k ≤ n, got k={k}, n={n}")
    max_rank = math.comb(n, k)
    if not 0 <= rank < max_rank:
        raise ValueError(f"rank must be in [0,{max_rank - 1}], got {rank}")

    combo: List[int] = []
    for i in range(k, 0, -1):
        # largest c such that C(c, i) ≤ rank (binary search)
        lo, hi = i - 1, n - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if math.comb(mid, i) <= rank:
                lo = mid
            else:
                hi = mid - 1
        combo.append(lo)
        rank -= math.comb(lo, i)
        n = lo  # next digit must be < current one
    combo.reverse()
    return combo


def _floyd_sample_indices(total: int, sample_size: int, *, rng: random.Random | None = None) -> Set[int]:
    """Select sample_size unique integers ∈ [0, total) uniformly at random"""
    if sample_size > total:
        raise ValueError("sample_size cannot exceed total")
    if rng is None:
        rng = random

    chosen: Set[int] = set()
    for j in range(total - sample_size, total):
        t = rng.randrange(0, j + 1)
        chosen.add(t if t not in chosen else j)
    return chosen


def _sample_exact_combinations(
    objects: Sequence[T], k: int, N: int, *, rng: random.Random | None = None
) -> List[List[T]]:
    """Draw N distinct k-combinations from objects exactly uniformly.

    The function first uses Bob Floyd to pick N distinct ranks in
    `[0, C(n,k))` (where `n = len(objects)`), then converts each rank to its
    combination via `_unrank_comb`, and finally maps the integer indices back
    to the actual objects.
    """
    n = len(objects)
    if not 0 <= k <= n:
        raise ValueError("require 0 ≤ k ≤ n")
    total = math.comb(n, k)
    if N > total:
        raise ValueError("cannot request more combinations than exist")
    if rng is None:
        rng = random

    ranks = _floyd_sample_indices(total, N, rng=rng)
    combos: List[List[T]] = []
    for r in ranks:
        idxs = _unrank_comb(n, k, r)
        combos.append([objects[i] for i in idxs])
    return combos


def create_cross_document_dataset(dataset: Dataset, stage_cfg: dict[str, Any]) -> Dataset:
    """Creates a cross-document Dataset by combining multi-hop chunks from different documents.

    Args:
        dataset: A HuggingFace Dataset where each row may contain a 'multihop_chunks' list and 'document_summary'.
        stage_cfg: Stage-specific config containing:
            - 'max_combinations' (int): The maximum number of cross-document combinations to generate.
            - 'chunks_per_document' (int): The number of chunks to sample from each document.
            - 'num_docs_per_combination' (List[int]): A list [min, max] specifying the range of documents to combine.
            - 'random_seed' (int): Seed for the random number generator.

    Returns:
        A new Dataset with cross-document combinations, preserving a similar schema but with an aggregated summary.
    """
    # Extract and validate configuration
    max_combinations = int(stage_cfg.get("max_combinations", 100))
    chunks_per_document = int(stage_cfg.get("chunks_per_document", 1))
    num_docs_range = stage_cfg.get("num_docs_per_combination", [2, 5])
    random_seed = int(stage_cfg.get("random_seed", 42))

    # Validate num_docs_range
    if not isinstance(num_docs_range, list) or len(num_docs_range) != 2:
        raise ValueError("num_docs_per_combination must be a list of exactly 2 integers")

    if not all(isinstance(x, int) for x in num_docs_range):
        raise ValueError("num_docs_per_combination must contain only integers")

    min_docs, max_docs = num_docs_range[0], num_docs_range[1]

    if min_docs < 2:
        raise ValueError("min_docs must be at least 2 for cross-document combinations")
    if max_docs < min_docs:
        raise ValueError("max_docs must be >= min_docs")

    if chunks_per_document < 1:
        raise ValueError("chunks_per_document must be at least 1")

    # Check for required column
    if "multihop_chunks" not in dataset.column_names:
        logger.warning("Dataset is missing 'multihop_chunks'. Cross-document generation aborted.")
        return Dataset.from_list([])

    # Extract documents with valid multihop_chunks
    docs = []
    for idx, row in enumerate(dataset):
        multihop_chunks = row.get("multihop_chunks", [])
        if isinstance(multihop_chunks, list) and multihop_chunks:
            valid_chunks = [
                chunk
                for chunk in multihop_chunks
                if isinstance(chunk, dict) and all(key in chunk for key in ("chunk_ids", "chunks_text"))
            ]
            if valid_chunks:
                # Create more readable and collision-resistant document IDs
                doc_id = row.get("document_id", f"doc_{idx}")
                # Clean doc_id for safe ID generation
                clean_doc_id = "".join(c for c in str(doc_id) if c.isalnum() or c in "_-")
                if not clean_doc_id:
                    clean_doc_id = f"doc_{idx}"

                docs.append({
                    "document_id": clean_doc_id,
                    "original_index": idx,
                    "document_summary": row.get("document_summary", ""),
                    "multihop_chunks": valid_chunks,
                })

    if len(docs) < min_docs:
        logger.warning(f"Found only {len(docs)} document(s) with valid 'multihop_chunks'. Need at least {min_docs}.")
        return Dataset.from_list([])

    logger.info(f"Found {len(docs)} documents with valid multihop_chunks")

    # Initialize random number generator
    rng = random.Random(random_seed)

    # Generate combinations efficiently using exact uniform sampling
    cross_rows = []

    # Strategy: distribute combinations across different group sizes
    # Calculate total possible combinations across all group sizes
    total_possible_combinations = sum(
        math.comb(len(docs), k) for k in range(min_docs, min(max_docs + 1, len(docs) + 1))
    )

    logger.info(f"Total possible combinations: {total_possible_combinations}")

    # Cap max_combinations to what's actually possible
    actual_max_combinations = min(max_combinations, total_possible_combinations)

    # For each possible number of documents to combine
    for num_docs_to_combine in range(min_docs, min(max_docs + 1, len(docs) + 1)):
        # Calculate how many combinations we can make with this number of docs
        combinations_for_this_size = math.comb(len(docs), num_docs_to_combine)

        if combinations_for_this_size == 0:
            continue

        # Determine how many combinations to generate for this group size
        remaining_combinations = actual_max_combinations - len(cross_rows)
        if remaining_combinations <= 0:
            break

        # Simple proportional allocation
        proportion = combinations_for_this_size / total_possible_combinations
        target_for_this_size = max(1, int(proportion * actual_max_combinations))
        actual_for_this_size = min(target_for_this_size, combinations_for_this_size, remaining_combinations)

        if actual_for_this_size <= 0:
            continue

        logger.info(f"Generating {actual_for_this_size} combinations with {num_docs_to_combine} documents")

        # Use exact uniform sampling to get distinct combinations
        try:
            doc_combinations = _sample_exact_combinations(docs, num_docs_to_combine, actual_for_this_size, rng=rng)
        except ValueError as e:
            logger.warning(f"Could not generate combinations for {num_docs_to_combine} docs: {e}")
            continue

        # Process each combination
        for doc_group in doc_combinations:
            sampled_chunks_from_group = []
            doc_ids_for_tracing = []

            # Sample chunks from each document in the group
            for doc in doc_group:
                doc_ids_for_tracing.append(doc["document_id"])

                if not doc["multihop_chunks"]:
                    continue

                # Sample the specified number of chunks from this document
                num_chunks_to_sample = min(chunks_per_document, len(doc["multihop_chunks"]))
                if num_chunks_to_sample == 1:
                    sampled_chunks = [rng.choice(doc["multihop_chunks"])]
                else:
                    sampled_chunks = rng.sample(doc["multihop_chunks"], num_chunks_to_sample)

                sampled_chunks_from_group.extend(sampled_chunks)

            # Validation: ensure we have chunks from the expected number of documents
            # (This addresses the original validation mismatch issue)
            expected_total_chunks = len(doc_group) * chunks_per_document
            if len(sampled_chunks_from_group) < len(doc_group):
                logger.warning(f"Insufficient chunks sampled from document group {doc_ids_for_tracing}")
                continue

            # Combine chunks from all documents in the group
            combined_ids = []
            combined_texts = []

            for chunk in sampled_chunks_from_group:
                chunk_ids = chunk.get("chunk_ids", [])
                chunk_texts = chunk.get("chunks_text", [])

                if isinstance(chunk_ids, list):
                    combined_ids.extend(chunk_ids)
                else:
                    combined_ids.append(chunk_ids)

                if isinstance(chunk_texts, list):
                    combined_texts.extend(chunk_texts)
                else:
                    combined_texts.append(chunk_texts)

            # Create combined multihop chunk
            combined_multihop_chunk = {
                "chunk_ids": combined_ids,
                "chunks_text": combined_texts,
            }

            # Combine document summaries
            doc_summaries = [
                doc["document_summary"]
                for doc in doc_group
                if doc.get("document_summary") and doc["document_summary"].strip()
            ]

            combined_summary = ""
            if doc_summaries:
                header = "Here are the summaries from the various documents involved in the chunking:"
                summary_bullets = "\n".join(f"- {s}" for s in doc_summaries)
                combined_summary = f"{header}\n\n{summary_bullets}"

            # Create readable and collision-resistant ID
            doc_ids_sorted = sorted(doc_ids_for_tracing)
            doc_ids_str = "_".join(doc_ids_sorted)

            # Create a human-readable, deterministic ID using number of documents, sorted document IDs, and chunks per document
            cross_doc_id = f"cross_{len(doc_group)}docs_{doc_ids_str}_chunks{chunks_per_document}"

            # Add comprehensive metadata for traceability
            metadata = {
                "source_documents": doc_ids_sorted,
                "num_source_docs": len(doc_group),
                "chunks_per_doc": chunks_per_document,
                "total_chunks_sampled": len(sampled_chunks_from_group),
                "source_indices": sorted([doc["original_index"] for doc in doc_group]),
                "generation_method": "exact_uniform_sampling",
            }

            cross_rows.append({
                "document_id": cross_doc_id,
                "document_summary": combined_summary,
                "chunks": [],  # keep consistent with original schema
                "multihop_chunks": [combined_multihop_chunk],
                "cross_document_metadata": metadata,  # add traceability
            })

    if not cross_rows:
        logger.warning("No cross-document combinations were generated.")
        return Dataset.from_list([])

    if len(cross_rows) < max_combinations:
        logger.info(f"Generated {len(cross_rows)} out of {max_combinations} requested combinations.")
    else:
        logger.info(f"Successfully generated {len(cross_rows)} cross-document combinations.")

    return Dataset.from_list(cross_rows)


# Dataset card generation functions


def extract_readme_metadata(repo_id: str, token: str | None = None) -> str:
    """Extracts the metadata from the README.md file of the dataset repository.
    We have to download the previous README.md file in the repo, extract the metadata from it.
    Args:
        repo_id: The ID of the repository to push to, from the `push_to_hub` method.
        token: The token to authenticate with the Hugging Face Hub, from the `push_to_hub` method.
    Returns:
        The metadata extracted from the README.md file of the dataset repository as a str.
    """
    try:
        import re
        from pathlib import Path

        from huggingface_hub.file_download import hf_hub_download

        readme_path = Path(hf_hub_download(repo_id, "README.md", repo_type="dataset", token=token))
        # Extract the content between the '---' markers
        metadata_match = re.findall(r"---\n(.*?)\n---", readme_path.read_text(), re.DOTALL)

        if not metadata_match:
            logger.debug("No YAML metadata found in the README.md")
            return ""

        return metadata_match[0]

    except Exception as e:
        logger.debug(f"Failed to extract metadata from README.md: {e}")
        return ""


def extract_dataset_info(repo_id: str, token: str | None = None) -> str:
    """
    Extract dataset_info section from README metadata.

    Args:
        repo_id: The dataset repository ID
        token: Optional HuggingFace token for authentication

    Returns:
        The dataset_info section as a string, or empty string if not found
    """
    readme_metadata = extract_readme_metadata(repo_id=repo_id, token=token)
    if not readme_metadata:
        return ""

    section_prefix = "dataset_info:"
    if section_prefix not in readme_metadata:
        return ""

    try:
        # Extract the part after `dataset_info:` prefix
        config_data = section_prefix + readme_metadata.split(section_prefix)[1]
        return config_data
    except IndexError:
        logger.debug("Failed to extract dataset_info section from metadata")
        return ""


def _serialize_config_for_card(config: dict[str, Any]) -> str:
    """
    Sanitize and serialize pipeline config to YAML for inclusion in dataset card.
    """
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML is required for config serialization")
    from copy import deepcopy

    def _sanitize(obj, key=None):
        if isinstance(obj, dict):
            return {k: _sanitize(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, str):
            # Keep placeholders
            if obj.startswith("$"):
                return obj
            # Mask only api_key arguments
            if key and "api_key" in key.lower():
                return "$API_KEY"
            # Mask OpenAI API keys
            if obj.startswith("sk-"):
                return "$OPENAI_API_KEY"
            # Mask HuggingFace tokens
            if obj.startswith("hf_"):
                return "$HF_TOKEN"
            return obj
        # Explicitly return boolean, integer, float, and None values unchanged
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        return obj

    sanitized = _sanitize(deepcopy(config))
    return yaml.safe_dump(sanitized, sort_keys=False, default_flow_style=False)


def _get_pipeline_subset_info(config: dict[str, Any]) -> str:
    """
    Generate a formatted markdown list of enabled pipeline stages with descriptions.
    The resulting markdown is used in the dataset card to document
    which processing steps were included in the pipeline.

    Args:
        config: The complete pipeline configuration dictionary containing
               the 'pipeline' section with enabled stages

    Returns:
        str: A markdown-formatted string with bullet points for each enabled pipeline stage,
             or an empty string if no stages are enabled
    """

    mapping = {
        "ingestion": "Read raw source documents, convert them to normalized markdown and save for downstream steps",
        "upload_ingest_to_hub": "Package and push ingested markdown dataset to the Hugging Face Hub or save locally with standardized fields",
        "summarization": "Perform hierarchical summarization: chunk-level LLM summaries followed by combine-stage reduction",
        "chunking": "Split texts into token-based single-hop and multi-hop chunks",
        "single_shot_question_generation": "Generate standalone question-answer pairs per chunk using LLM",
        "multi_hop_question_generation": "Generate multi-hop QA pairs requiring reasoning across multiple chunks",
        "lighteval": "Merge QA pairs and chunk metadata into a lighteval compatible dataset for quick model-based scoring",
        "citation_score_filtering": "Compute overlap-based citation scores and filter QA pairs accordingly",
    }
    pipeline = config.get("pipeline", {})
    lines = []
    for stage, cfg in pipeline.items():
        if isinstance(cfg, dict) and cfg.get("run"):
            desc = mapping.get(stage, stage.replace("_", " ").title())
            lines.append(f"- **{stage}**: {desc}")
    return "\n".join(lines)


def _generate_and_upload_dataset_card(config: dict[str, Any], template_path: str | None = None) -> None:
    """
    Internal implementation that generates and uploads a dataset card to Hugging Face Hub.

    This is the core implementation function called by the public upload_dataset_card() function.
    It handles the actual card generation and uploading without performing configuration checks.

    The dataset card includes:
    1. Pipeline subset descriptions based on enabled stages
    2. Full sanitized configuration for reproducibility
    3. YourBench version and other metadata
    4. Preserved dataset_info from the existing card for proper configuration display

    Args:
        config: Configuration dictionary containing HF settings
        template_path: Optional custom template path
    """
    logger.info("Starting dataset card upload process")

    if _is_offline():
        logger.warning("Offline mode enabled. Skipping dataset card upload.")
        return

    try:
        # Get dataset repo name
        settings = _extract_settings(config)
        dataset_repo_name = settings.repo_id
        logger.info(f"Uploading card for dataset: {dataset_repo_name}")

        # Load template
        if not template_path:
            # Try to find template in utils directory
            current_dir = os.path.dirname(__file__)
            template_path = os.path.join(current_dir, "yourbench_card_template.md")

        logger.info(f"Loading template from: {template_path}")

        if not os.path.exists(template_path):
            logger.error(f"Template file not found: {template_path}")
            return

        with open(template_path, "r", encoding="utf-8") as f:
            template_str = f.read()

        logger.debug(f"Template loaded successfully, length: {len(template_str)} characters")

        # Get HF token
        token = settings.token

        # Extract dataset_info section from existing README if available
        config_data = extract_dataset_info(repo_id=dataset_repo_name, token=token)
        logger.info(f"Extracted dataset_info section, length: {len(config_data) if config_data else 0} characters")

        # Use explicitly configured pretty_name or generate one from the dataset name
        hf_config = config.get("hf_configuration", {})
        if "pretty_name" in hf_config:
            pretty_name = hf_config["pretty_name"]
        else:
            dataset_name = dataset_repo_name.split("/")[-1]
            pretty_name = dataset_name.replace("-", " ").replace("_", " ").title()

        card_data_kwargs = {"pretty_name": pretty_name}

        # Create DatasetCardData with our metadata
        card_data = DatasetCardData(**card_data_kwargs)
        logger.info(f"Created card data with pretty_name: {card_data.pretty_name}")

        # Get YourBench version
        from importlib.metadata import PackageNotFoundError, version

        try:
            version_str = version("yourbench")
        except PackageNotFoundError:
            # Fallback for development installs
            version_str = "dev"

        # Prepare template variables
        template_vars = {
            "pretty_name": card_data.pretty_name,
            "yourbench_version": version_str,
            "config_yaml": _serialize_config_for_card(config),
            "pipeline_subsets": _get_pipeline_subset_info(config),
            "config_data": config_data,  # Use the extracted dataset_info section
            "footer": hf_config.get("footer", "*(This dataset card was automatically generated by YourBench)*"),
        }

        logger.info("Rendering dataset card from template")
        logger.debug(f"Template variables: {list(template_vars.keys())}")

        # Render card with our template and variables
        card = DatasetCard.from_template(card_data=card_data, template_str=template_str, **template_vars)

        logger.info("Template rendered successfully")
        logger.debug(f"Rendered card content length: {len(str(card))} characters")

        # Push to hub
        logger.info(f"Pushing dataset card to hub: {dataset_repo_name}")
        card.push_to_hub(dataset_repo_name, token=token)

        logger.success(f"Dataset card successfully uploaded to: https://huggingface.co/datasets/{dataset_repo_name}")

    except Exception as e:
        logger.error(f"Failed to upload dataset card: {e}")
        logger.exception("Full traceback:")


def upload_dataset_card(config: dict[str, Any]) -> None:
    """
    Public interface to generate and upload a dataset card to Hugging Face Hub.

    This function performs configuration checks (like upload_card setting and offline mode)
    and then delegates to the internal _generate_and_upload_dataset_card() implementation.
    It should be called at the end of the pipeline when all subsets are available.

    Args:
        config: Pipeline configuration dictionary containing 'hf_configuration'
               with settings like 'upload_card' flag
    """
    try:
        # Check if card upload is enabled in config
        hf_config = config.get("hf_configuration", {})
        upload_card = hf_config.get("upload_card", True)

        if not upload_card:
            logger.info("Dataset card upload disabled in configuration. Skipping card upload.")
            return

        if _is_offline():
            logger.info("Offline mode enabled. Skipping dataset card upload.")
            return

        logger.info("Uploading dataset card with complete pipeline information")
        _generate_and_upload_dataset_card(config)

    except Exception as e:
        logger.error(f"Error uploading dataset card: {e}")
