"""One real JEV format smoke check for the AI Bot instruction path."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_evaluation
import evaluation
import server


def main():
    server.load_env_file()
    case = {"category": "일반지식·설명", "prompt": "도움말을 한 문장으로 알려줘.",
            "conversation_history": "", "attachment_text": "",
            "evaluation_spec_json": "{}"}
    response = {"content": "안녕하세요. 도움말입니다."}
    state, questions = evaluation.prepare(case, response)
    bot_evaluation.add_questions(state, questions, "답변을 시작할 때 인사한다.")
    raw = server.call_jev(state, "jev-1.13.0", questions)
    evaluation.parse_result(raw, state, questions)
    assessment = bot_evaluation.parse_result(raw, state)
    print(json.dumps({"botStatus": assessment["status"], "verdict": assessment["rules"][0]["verdict"],
                      "answerRef": assessment["rules"][0]["answerRef"], "questionCount": len(questions)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
