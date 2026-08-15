# Telegram GroupScan အသုံးပြုနည်းလမ်းညွှန်

## ၁။ GroupScan ဆိုတာဘာလဲ

`GroupScan` သည် Telegram group များ၏ **အမည်၊ description နှင့် member count** ကို user ကပေးသွင်းသည့် metadata အပေါ်အခြေခံပြီး target niche နှင့် ကိုက်ညီမှုကို စစ်ဆေးပေးသော feature ဖြစ်ပါတယ်။ ရလဒ်အနေဖြင့် group တစ်ခုချင်းစီကို `TARGET`, `REVIEW` သို့မဟုတ် `EXCLUDE` အဖြစ် အကြံပြုပေးပြီး spam signal နှင့် relevance evidence များကိုလည်း ဖော်ပြပေးပါတယ်။

> GroupScan သည် group များကို အလိုအလျောက်ရှာဖွေခြင်း၊ join ဝင်ခြင်း၊ message ပို့ခြင်း သို့မဟုတ် အဖွဲ့၏ တကယ့် engagement ကို တိုင်းတာခြင်း မလုပ်ပါ။ User ပေးထားသည့် အချက်အလက်များကိုသာ အကဲဖြတ်ပါတယ်။

## ၂။ မစတင်မီ လိုအပ်ချက်များ

Bot သည် run နေရပါမယ်။ AI provider profile မထည့်ရသေးပါက admin သည် private chat မှာ အောက်ပါအတိုင်း provider ကို အရင်ပြင်ဆင်ပါ။

```text
/provider_add
/provider_test <provider_name>
/provider_use <provider_name>
```

လက်ရှိ active provider ကို စစ်လိုပါက `/provider_list` ကို အသုံးပြုနိုင်ပါတယ်။ Provider pool မသုံးဘဲ environment variable ဖြင့် တိုက်ရိုက်ချိတ်ထားပါက `LLM_API_KEY`, `LLM_BASE_URL` နှင့် `LLM_MODEL` သတ်မှတ်ထားရုံနဲ့ ရပါတယ်။

GroupScan ကို chat တချို့မှာသာ ခွင့်ပြုထားလျှင် `GROUPSCAN_ALLOWED_CHAT_IDS` environment variable ထဲတွင် သတ်မှတ်ထားသော chat များအတွင်းကသာ command အလုပ်လုပ်ပါမယ်။ လက်ရှိ chat ID ကို သိလိုပါက group ထဲမှာ အောက်ပါ command ကိုသုံးပါ။

```text
/id
```

## ၃။ အဓိက command များ

| Command | အသုံးပြုပုံ | ရည်ရွယ်ချက် |
|---|---|---|
| `/groupscan <niche>` | `/groupscan AI tools` | Group list ကို သတ်မှတ်ထားသော niche နှင့် စစ်ရန် |
| `/scout <niche>` | `/scout digital marketing` | `/groupscan` ၏ backward-compatible alias ဖြစ်သည် |
| `/id` | `/id` | လက်ရှိ Telegram chat ID ကို ပြရန် |
| `/help` | `/help` | Bot command များအားလုံးကို ပြရန် |

အကြံပြုထားသော command သည် `/groupscan` ဖြစ်ပြီး `/scout` သည် အရင်အသုံးပြုနေသူများအတွက် ဆက်လက်ထားရှိသည့် alias ဖြစ်ပါတယ်။

## ၄။ Inline text ဖြင့် စစ်ဆေးခြင်း

Command နောက်တွင် ပထမစာကြောင်းကို target niche အဖြစ် ထည့်ပြီး နောက်စာကြောင်းများမှာ group record များဖြစ်ရပါမယ်။ Pipe `|` သုံးပြီး format ကို ခွဲပါ။

```text
/groupscan AI tools
AI Myanmar | မြန်မာဘာသာ AI tools နှင့် productivity ဆွေးနွေးခြင်း | 12K
Marketing MM | Digital marketing နှင့် ads | 850
Crypto Deals | token giveaways နှင့် instant profit | 45K
```

Record format မှာ အောက်ပါအတိုင်းဖြစ်ပါတယ်။

```text
Group Name | Group Description | Member Count
```

`12K`, `1.5M`, `850` နှင့် comma ပါသော `12,000` ကဲ့သို့သော member count များကို bot က နားလည်နိုင်ပါတယ်။ Member count မသိပါက အလွတ်ထားနိုင်သော်လည်း quality score ကို `unknown` သို့မဟုတ် `review` အဖြစ် ပြန်လာနိုင်ပါတယ်။

## ၅။ ရှိပြီးသား message ကို reply လုပ်ပြီး စစ်ဆေးခြင်း

Group list သို့မဟုတ် group metadata ပါသော message တစ်ခုကို reply လုပ်ပြီး command သုံးနိုင်ပါတယ်။

```text
/groupscan AI tools
```

Niche မထည့်ဘဲ list message ကို reply လုပ်ပြီး `/groupscan` သုံးပါက bot သည် ပေးထားသော data မှ context ကို အသုံးပြုပြီး စစ်ဆေးပါမယ်။ သို့သော် niche ကို တိတိကျကျ ထည့်ပေးခြင်းက ပိုမိုတိကျသော relevance score ရရှိစေပါတယ်။

## ၆။ Text, CSV သို့မဟုတ် JSON file upload ဖြင့် စစ်ဆေးခြင်း

Group list အများကြီးရှိပါက UTF-8 encoded `.txt`, `.csv` သို့မဟုတ် `.json` file ကို Bot ထံ upload လုပ်ပါ။ ထို့နောက် file message ကို reply လုပ်ပြီး အောက်ပါ command ကိုသုံးပါ။

```text
/groupscan AI tools
```

### CSV format

CSV file တွင် header row ပါရပါမယ်။ အောက်ပါ column names များကို အသုံးပြုနိုင်ပါတယ်။

```csv
name,description,members
AI Myanmar,AI tools ဆွေးနွေးခြင်း,12K
Marketing MM,Digital marketing နှင့် ads,850
```

`name` အစား `group_name`၊ `description` အစား `bio`၊ `members` အစား `member_count` ကိုလည်း အသုံးပြုနိုင်ပါတယ်။

### JSON format

JSON file သို့မဟုတ် message ကို အောက်ပါပုံစံနဲ့ ထည့်နိုင်ပါတယ်။

```json
{
  "groups": [
    {
      "name": "AI Myanmar",
      "description": "မြန်မာဘာသာ AI tools ဆွေးနွေးခြင်း",
      "member_count": "12K"
    },
    {
      "name": "Marketing MM",
      "description": "Digital marketing နှင့် ads",
      "member_count": 850
    }
  ]
}
```

JSON array တိုက်ရိုက်ပုံစံကိုလည်း လက်ခံပါတယ်။

```json
[
  {"name": "AI Myanmar", "description": "AI tools", "member_count": "12K"}
]
```

## ၇။ Report ကို ဘယ်လိုဖတ်မလဲ

GroupScan report တွင် group တစ်ခုချင်းစီအတွက် အောက်ပါအချက်များ ပါဝင်ပါတယ်။

| Field | အဓိပ္ပာယ် |
|---|---|
| `0–100 score` | Target niche နှင့် content relevance ကိုသာ အခြေခံထားသော fit score ဖြစ်သည်။ Popularity score မဟုတ်ပါ။ |
| `TARGET` | ပေးထားသော metadata အပေါ်အခြေခံ၍ target list ထဲသို့ ထည့်ရန် သင့်လျော်နိုင်သည်။ |
| `REVIEW` | Information မလုံလောက်ခြင်း သို့မဟုတ် relevance/quality မသေချာခြင်းကြောင့် လူက ပြန်စစ်ရန်လိုသည်။ |
| `EXCLUDE` | Niche မကိုက်ညီခြင်း၊ spam signal ရှိခြင်း သို့မဟုတ် မသင့်လျော်သော group ဖြစ်နိုင်ခြေကြောင့် မထည့်သင့်ပါ။ |
| `SPAM FLAG` | Input description သို့မဟုတ် name ထဲတွင် spam ဆန်သော signal တွေ့ရှိထားသည်။ |
| `IRRELEVANT` | Target niche နှင့် မကိုက်ညီသော signal ရှိသည်။ |
| `HIGH / MEDIUM / LOW / UNKNOWN` | ပေးထားသော input က ပြသထားသည့် quality signal အဆင့်ဖြစ်သည်။ `UNKNOWN` ဆိုသည်မှာ လုံလောက်သော evidence မရှိခြင်းဖြစ်သည်။ |
| `Evidence` | Score သို့မဟုတ် action ပြုလုပ်ရာတွင် model က အသုံးပြုထားသော supplied input အပိုင်းများဖြစ်သည်။ |

ဥပမာ report တစ်ခုမှာ အောက်ပါအတိုင်း ပြနိုင်ပါတယ်။

```text
• AI Myanmar — 90/100 | HIGH | TARGET
  Niche description နှင့် တိုက်ရိုက်ကိုက်ညီသည်။
  Evidence: AI tools; မြန်မာဘာသာဆွေးနွေးခြင်း

• Crypto Deals — 10/100 | LOW | EXCLUDE | SPAM FLAG
  Instant profit နှင့် giveaway ဆန်သော description ဖြစ်သည်။
  Evidence: instant profit; giveaways
```

Report ထဲတွင် `TARGET` ဖြစ်လာသော်လည်း bot သည် group ကို အလိုအလျောက် join ဝင်ခြင်း၊ target list ထဲသို့ auto-save လုပ်ခြင်း သို့မဟုတ် message ပို့ခြင်း မလုပ်ပါ။ လူက final review ပြုလုပ်ပြီး သင့်တော်သော action ကို ကိုယ်တိုင်ဆုံးဖြတ်ရပါမယ်။

## ၈။ အကောင်းဆုံးအသုံးပြုနည်း

ပထမဦးစွာ target niche ကို တိတိကျကျ သတ်မှတ်ပါ။ ဥပမာ `AI tools for Myanmar creators` သို့မဟုတ် `Burmese digital marketing` ကဲ့သို့ ရေးပါ။ ထို့နောက် group name, description နှင့် member count ကို source တစ်ခုတည်းမှ စုစည်းပြီး format တူအောင် ပြင်ဆင်ပါ။ GroupScan ပြီးလျှင် `TARGET` ကိုသာ ချက်ချင်းမယုံဘဲ description နှင့် evidence ကို ပြန်ဖတ်ပါ။ `REVIEW` ဖြစ်သော group များကို လူက ထပ်မံစစ်ဆေးပြီး `EXCLUDE` နှင့် `SPAM FLAG` များကို မသုံးသင့်ပါ။

Member count အများကြီးရှိခြင်းသည် engagement ကောင်းသည်ဟု မဆိုလိုပါ။ ထို့ကြောင့် GroupScan ကို **အကြို filter** အဖြစ် အသုံးပြုပြီး final audience-quality decision ကို လူကသာ ပြုလုပ်သင့်ပါတယ်။

## ၉။ Common errors နှင့် ဖြေရှင်းနည်း

| Error | ဖြေရှင်းနည်း |
|---|---|
| `Group record မတွေ့ပါ` | `Group Name | Description | Member Count` format ကို စစ်ပါ။ JSON သုံးပါက `name` field မဖြစ်မနေပါရမယ်။ |
| `Group format မမှန်ပါ` | Pipe `|` သို့မဟုတ် valid CSV/JSON format သုံးပါ။ File သည် UTF-8 ဖြစ်ရပါမယ်။ |
| `ဒီ chat ကို GroupScan အသုံးပြုခွင့် allowlist ထဲတွင် မထည့်ရသေးပါ` | `/id` ဖြင့် chat ID ရယူပြီး `GROUPSCAN_ALLOWED_CHAT_IDS` ထဲထည့်ပါ။ |
| `The language model request failed` | `/provider_test <name>` ဖြင့် API profile စစ်ပါ။ Endpoint, API key, model ID နှင့် response format setting ကို ပြန်စစ်ပါ။ |
| `GroupScan result did not contain...` | Model output မပြည့်စုံခြင်းဖြစ်နိုင်ပါတယ်။ Request ကို အုပ်စုနည်းနည်းခွဲပြီး ပြန်စမ်းပါ။ |
| Result အားလုံး `REVIEW` ဖြစ်နေခြင်း | Description မလုံလောက်ခြင်း သို့မဟုတ် niche မရှင်းခြင်းဖြစ်နိုင်ပါတယ်။ Group description နှင့် target niche ကို ပိုတိကျအောင် ထည့်ပါ။ |

## ၁၀။ လက်ရှိကန့်သတ်ချက်များ

Default setting အရ scan တစ်ကြိမ်တွင် group အများဆုံး `50` ခုနှင့် UTF-8 input file အများဆုံး `1,000,000` bytes ကို လက်ခံပါတယ်။ `GROUPSCAN_MAX_GROUPS` နှင့် `GROUPSCAN_MAX_FILE_BYTES` environment variables ဖြင့် ပြောင်းနိုင်ပါတယ်။

GroupScan သည် user ပေးထားသော metadata ကိုသာ အသုံးပြုသောကြောင့် actual member activity, recent posts, engagement rate, admin quality, group privacy status သို့မဟုတ် real-time membership ကို အတည်ပြုမပေးနိုင်ပါ။ မပေးထားသော facts များကို bot က မဖန်တီးစေရန် design လုပ်ထားပါတယ်။

---

**အကျဉ်းချုပ်:** Target niche ကို အရင်သတ်မှတ်ပါ၊ group metadata ကို format တူအောင် စုစည်းပါ၊ `/groupscan <niche>` ဖြင့် စစ်ပါ၊ ပြီးလျှင် `TARGET / REVIEW / EXCLUDE` နှင့် evidence ကို လူက final review လုပ်ပါ။
