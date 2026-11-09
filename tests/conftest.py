import pytest


@pytest.fixture(autouse=True)
def _no_real_llm_judge(monkeypatch):
    """排序的 LLM 評分預設關掉，測試不准真的呼叫 claude。stub 放在最底層（humanjudge.run_cmd 回失敗），
    所以就算測試中途 monkeypatch.undo()，judge 也只會得到失敗結果（回 None = 退回關鍵詞分數），不會真的開 claude。"""
    from src import humanjudge
    monkeypatch.setattr(humanjudge, "run_cmd", lambda *a, **k: (1, "", "test stub: no real claude"))
