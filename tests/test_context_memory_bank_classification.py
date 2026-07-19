from context.language import MemoryBankDetector


def test_all_markdown_files_are_memory_bank_resources():
    for path in ("README.md", "memory-bank/plan.md", "notes/deep/topic.mdx", "src/guide.markdown"):
        language, is_memory_bank = MemoryBankDetector.detect_language(path)
        assert language == "memory_bank"
        assert is_memory_bank is True


def test_non_markdown_files_are_not_memory_bank_resources():
    assert MemoryBankDetector.detect_language("memory-bank/settings.json") == ("json", False)
