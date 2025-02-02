from dateutil.parser import parse
from beancount.ingest import importer
from beancount.core import data, amount
from beancount.core.number import D

import re
import sys
import fitz


from china_bean_importers.common import *


class Importer(importer.ImporterProtocol):
    def __init__(self, config) -> None:
        super().__init__()
        self.config = config

    def identify(self, file):
        return "PDF" in file.name.upper() and "中国银行信用卡" in file.name

    def file_account(self, file):
        return "boc_credit_card"

    def file_date(self, file):
        doc = fitz.open(file.name)
        begin = False
        page = doc[0]
        text = page.get_text("blocks")
        for (x0, y0, x1, y1, content, block_no, block_type) in text:
            content = content.strip()
            if not begin and "Current FCY Total Balance Due" in content:
                begin = True
            elif begin:
                parts = content.split('\n')
                if len(parts) == 4:
                    return parse(parts[1])
                else:
                    break
        return super().file_date(file)

    def extract(self, file, existing_entries=None):
        entries = []
        doc = fitz.open(file.name)
        card_number = None
        begin = False
        lineno = 0

        for i in range(doc.page_count):
            page = doc[i]
            text = page.get_text("blocks")
            for (x0, y0, x1, y1, content, block_no, block_type) in text:
                lineno += 1
                content = content.strip()
                if block_type != 0: # 0: text, 1: image
                    continue
                if re.match(r'(第 [0-9]+ 页/共)|([0-9]+ 页)', content):
                    continue

                if "人民币交易明细" in content:
                    currency = "CNY"
                elif m := re.match(r'\((\w+)\)外币交易明细', content):
                    currency = m.group(1)
                match = re.search(r'卡号：([0-9]+)', content)
                if match:
                    card_number = int(match[1])
                    begin = False
                elif card_number:
                    if not begin and "Expenditure" in content:
                        begin = True
                    elif begin and ("Loyalty Plan" in content or "交易日" in content):
                        begin = False
                    elif begin:
                        # Is it a date line?
                        # card number can be empty
                        if re.match(r'[0-9]+-[0-9]+-[0-9]+\n[0-9]+-[0-9]+-[0-9]+(\n[0-9]+)?', content, re.MULTILINE):
                            date = parse(content.split('\n')[0]).date()
                            description = ""
                        else:
                            # Otherwise: Description/Deposit/Expenditure
                            description += content + "\n"
                            done = False
                            if x1 > 500:
                                # Expenditure found
                                expense = True
                                done = True
                            elif x1 > 400:
                                # Deposit found
                                expense = False
                                done = True
                            if done:
                                orig_narration = "".join(
                                    description.split("\n")[:-2])
                                value = description.split("\n")[-2]
                                units = amount.Amount(D(value), currency)

                                if '-' in orig_narration:
                                    hypen_idx = orig_narration.index('-')
                                    narration, payee = orig_narration[:hypen_idx].strip(), orig_narration[hypen_idx+1:].strip()
                                else:
                                    narration = orig_narration
                                    payee = None

                                metadata = data.new_metadata(file.name, lineno)
                                tags = set()
                                account1 = find_account_by_card_number(self.config, card_number)
                                my_assert(account1, f"Unknown card number {card_number}", lineno, None)

                                if in_blacklist(self.config, orig_narration):
                                    print(f"Item skipped due to blacklist: {date} {orig_narration} [{units}]", file=sys.stderr)
                                    continue

                                if expense:
                                    units = -units

                                if m := match_destination_and_metadata(self.config, orig_narration, payee): # match twice with narration
                                    (account2, new_meta, new_tags) = m
                                    metadata.update(new_meta)
                                    tags = tags.union(new_tags)
                                if account2 is None:
                                    account2 = unknown_account(
                                        self.config, expense)

                                if '授权批准' in narration: # 还款
                                    tags.add('maybe-repayment')
                                # Assume transfer from the first debit card?
                                # if "Bank of China Mobile Client" in narration and units1.number > 0:
                                #     account2 = f"Assets:Card:BoC:{self.config['card_accounts']['Assets:Card']['BoC'][0]}"
                                #     # Swap for deduplication
                                #     account1, account2 = account2, account1
                                #     units1 = -units1

                                txn = data.Transaction(
                                    meta=metadata, date=date, flag=self.FLAG, payee=payee, narration=narration, tags=tags, links=data.EMPTY_SET, postings=[
                                        data.Posting(account=account1, units=units,
                                                     cost=None, price=None, flag=None, meta=None),
                                        data.Posting(account=account2, units=None,
                                                     cost=None, price=None, flag=None, meta=None),
                                    ])
                                entries.append(txn)
        return entries
