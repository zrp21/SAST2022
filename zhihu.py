from cgitb import text
from email import header
import requests
import json
import pymysql
from bs4 import BeautifulSoup as BS
import re
import logging
import time

fmt = '%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s'
datefmt = '%Y-%m-%d %H:%M:%S'
level = logging.INFO

formatter = logging.Formatter(fmt, datefmt)
logger = logging.getLogger()
logger.setLevel(level)

file = logging.FileHandler("../zhihu.log", encoding='utf-8')
file.setLevel(level)
file.setFormatter(formatter)
logger.addHandler(file)

console = logging.StreamHandler()
console.setLevel(level)
console.setFormatter(formatter)
logger.addHandler(console)


class ZhihuCrawler:
    def __init__(self):
        with open("zhihu.json", "r", encoding="utf8") as f:
            self.settings = json.load(f)  # Load settings
        logger.info("Settings loaded")


    def sleep(self, sleep_key, delta=0):
        """
        Execute sleeping for a time configured in the settings

        :param sleep_key: the sleep time label
        :param delta: added to the sleep time
        :return:
        """
        _t = self.settings["config"][sleep_key] + delta
        logger.info(f"Sleep {_t} second(s)")
        time.sleep(_t)

    def query(self, sql, args=None, op=None):
        """
        Execute an SQL query

        :param sql: the SQL query to execute
        :param args: the arguments in the query
        :param op: the operation to cursor after query
        :return: op(cur)
        """
        conn = pymysql.connect(
            cursorclass=pymysql.cursors.DictCursor,
            client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS,
            **self.settings['mysql']
        )
        if args and not (isinstance(args, tuple) or isinstance(args, list)):
            args = (args,)
        with conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql, args)
                    conn.commit()
                    if op is not None:
                        return op(cur)
                except:  # Log query then exit
                    if hasattr(cur, "_last_executed"):
                        logger.error("Exception @ " + cur._last_executed)
                    else:
                        logger.error("Exception @ " + sql)
                    raise

    def watch(self, top=None):
        """
        The crawling flow

        :param top: only look at the first `top` entries in the board. It can be used when debugging
        :return:
        """
        self.create_table()
        while True:
            logger.info("Begin crawling ...")
            try:
                crawl_id = None
                begin_time = time.time()
                crawl_id = self.begin_crawl(begin_time)

                try:
                    board_entries = self.get_board()
                except RuntimeError as e:
                    if isinstance(e.args[0], requests.Response):
                        logger.exception(e.args[0].status_code, e.args[0].text)
                    raise
                else:
                    logger.info(
                        f"Get {len(board_entries)} items: {','.join(map(lambda x: x['title'][:20], board_entries))}")
                if top:
                    board_entries = board_entries[:top]

                # Process each entry in the hot list
                for idx, item in enumerate(board_entries):
                    self.sleep("interval_between_question")
                    detail = {
                        "created": None,
                        "visitCount": None,
                        "followerCount": None,
                        "answerCount": None,
                        "raw": None,
                        "hit_at": None
                    }
                    if item["qid"] is None:
                        logger.warning(f"Unparsed URL @ {item['url']} ranking {idx} in crawl {crawl_id}.")
                    else:
                        try:
                            detail = self.get_question(item["qid"])
                        except Exception as e:
                            if len(e.args) > 0 and isinstance(e.args[0], requests.Response):
                                logger.exception(f"{e}; {e.args[0].status_code}; {e.args[0].text}")
                            else:
                                logger.exception(f"{str(e)}")
                        else:
                            logger.info(f"Get question detail for {item['title']}: raw detail length {len(detail['raw']) if detail['raw'] else 0}")
                    try:
                        self.add_entry(crawl_id, idx, item, detail)
                    except Exception as e:
                        logger.exception(f"Exception when adding entry {e}")
                self.end_crawl(crawl_id)
            except Exception as e:
                logger.exception(f"Crawl {crawl_id} encountered an exception {e}. This crawl stopped.")
            self.sleep("interval_between_board", delta=(begin_time - time.time()))

    def create_table(self):
        """
        Create tables to store the hot question records and crawl records

        """
        sql = f"""
CREATE TABLE IF NOT EXISTS `crawl` (
    `id` BIGINT NOT NULL AUTO_INCREMENT,
    `begin` DOUBLE NOT NULL,
    `end` DOUBLE,
    PRIMARY KEY (`id`) USING BTREE
)
AUTO_INCREMENT = 1 
CHARACTER SET = utf8mb4 
COLLATE = utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `record`  (
    `id` BIGINT NOT NULL AUTO_INCREMENT,
    `qid` INT NOT NULL,
    `crawl_id` BIGINT NOT NULL,
    `hit_at` DOUBLE,
    `ranking` INT NOT NULL,
    `title` VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL ,
    `heat` VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
    `created` INT,
    `visitCount` INT,
    `followerCount` INT,
    `answerCount` INT,
    `excerpt` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
    `raw` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci ,
    `url` VARCHAR(255),
    PRIMARY KEY (`id`) USING BTREE,
    INDEX `CrawlAssociation` (`crawl_id`) USING BTREE,
    CONSTRAINT `CrawlAssociationFK` FOREIGN KEY (`crawl_id`) REFERENCES `crawl` (`id`)
) 
AUTO_INCREMENT = 1 
CHARACTER SET = utf8mb4 
COLLATE = utf8mb4_unicode_ci;

"""
        self.query(sql)

    def begin_crawl(self, begin_time) -> (int, float):
        """
        Mark the beginning of a crawl
        :param begin_time:
        :return: (Crawl ID, the time marked when crawl begin)
        """
        sql = """
INSERT INTO crawl (begin) VALUES(%s);
"""
        return self.query(sql, begin_time, lambda x: x.lastrowid)

    def end_crawl(self, crawl_id: int):
        """
        Mark the ending time of a crawl

        :param crawl_id: Crawl ID
        """
        sql = """
UPDATE crawl SET end = %s WHERE id = %s;
"""
        self.query(sql, (time.time(), crawl_id))

    def add_entry(self, crawl_id, idx, board, detail):
        """
        Add a question entry to database

        :param crawl_id: Crawl ID
        :param idx: Ranking in the board
        :param board: dict, info from the board
        :param detail: dict, info from the detail page
        """
        sql = \
            """
INSERT INTO record (`qid`, `crawl_id`, `title`, `heat`, `created`, `visitCount`, `followerCount`, `answerCount`,`excerpt`, `raw`, `ranking`, `hit_at`, `url`)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
"""
        self.query(
            sql,
            (
                board["qid"],
                crawl_id,
                board["title"],
                board["heat"],
                detail["created"],
                detail["visitCount"],
                detail["followerCount"],
                detail["answerCount"],
                board["excerpt"],
                detail["raw"],
                idx,
                detail["hit_at"],
                board["url"]
            )
        )

    def get_board(self) -> list:
        """
        TODO: Fetch current hot questions

        :return: hot question list, ranking from high to low

        Return Example:
        [
            {
                'title': '针对近期生猪市场非理性行为，国家发展改革委研究投放猪肉储备，此举对市场将产生哪些积极影响？',
                'heat': '76万热度',
                'excerpt': '据国家发展改革委微信公众号 7 月 5 日消息，针对近期生猪市场出现盲目压栏惜售等非理性行为，国家发展改革委价格司正研究启动投放中央猪肉储备，并指导地方适时联动投放储备，形成调控合力，防范生猪价格过快上涨。',
                'url': 'https://www.zhihu.com/question/541600869',
                'qid': 541600869,
            },
            {
                'title': '有哪些描写夏天的古诗词？',
                'heat': '41万热度',
                'excerpt': None,
                'url': 'https://www.zhihu.com/question/541032225',
                'qid': 541032225,
            },
            {
                'title':    # 问题标题
                'heat':     # 问题热度
                'excerpt':  # 问题摘要
                'url':      # 问题网址
                'qid':      # 问题编号
            }
            ...
        ]
        """

        # Hint: - Parse HTML, pay attention to the <section> tag.
        #       - Use keyword argument `class_` to specify the class of a tag in `find`
        #       - Hot Question List can be accessed in https://www.zhihu.com/hot
        
        resp =requests.get("https://www.zhihu.com/hot", headers={"User-Agent":self.settings["headers"]["User-Agent"],"Cookie":self.settings["headers"]["Cookie"]})
        soup = BS(resp.text, 'lxml')
        divs = soup.find_all('section',class_='HotItem')
        data = []
        for each in divs:
            heat = each.find('div', class_='HotItem-metrics').text
            title = each.find('h2', class_='HotItem-title').text
            connection = each.find('a')['href']
            qid=re.match("https://www.zhihu.com/question/(\d+)",connection)
            if qid == None:
                qid = 'None'
            else:
                qid=qid.group(1)
            excerpt =each.find('p',class_='HotItem-excerpt')
            if excerpt is None:
                excerpt='None'
            else:
                excerpt=excerpt.text
            dict = {'qid': qid, 'title': title,
                    'heat': heat, 'url': connection,'excerpt':excerpt}
            # print(dict)
            data.append(dict)
        return data
        raise NotImplementedError

    def get_question(self, qid: int) -> dict:
        """
        TODO: Fetch question info by question ID

        :param qid: Question ID
        :return: a dict of question info

        Return Example:
        {
            "created": 1657248657,      # 问题的创建时间
            "followerCount": 5980,      # 问题的关注数量
            "visitCount": 2139067,      # 问题的浏览次数
            "answerCount": 2512         # 问题的回答数量
            "title": "日本前首相安倍      # 问题的标题
                晋三胸部中枪已无生命
                体征 ，嫌疑人被控制，
                目前最新进展如何？背
                后原因为何？",
            "raw": "<p>据央视新闻，        # 问题的详细描述
                当地时间8日，日本前
                首相安倍晋三当天上午
                在奈良发表演讲时中枪
                。据悉，安倍晋三在上
                救护车时还有意。。。",
            "hit_at": 1657264954.3134503  # 请求的时间戳
        }


        """

        # Hint: - Parse JSON, which is embedded in a <script> and contains all information you need.
        #       - After find the element in soup, use `.text` attribute to get the inner text
        #       - Use `json.loads` to convert JSON string to `dict` or `list`
        #       - You may first save the JSON in a file, format it and locate the info you need
        #       - Use `time.time()` to create the time stamp
        #       - Question can be accessed in https://www.zhihu.com/question/<Question ID>
        resp=requests.get("https://www.zhihu.com/question/"+qid,headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.5060.114 Safari/537.36","Cookie":"__snaker__id=beqXSUkXTwP5PlFT; SESSIONID=yHdTQV5JSTfAIoGaUwc6Y1Hxdx1l6iikMOnXICsasIx; JOID=UFARAk5iQmKXU8geJGFfex3_rZUxAn8-0BCrK1YJDhrfY4FiG7ihu_ZezhclHRwwE0GodQ9DgMx3XTnmWa4IEAk=; osd=W1gdAklpSm6XVMMWKGFYcBXzrZI6CnM-1xujJ1YOBRLTY4ZpE7ShvP1WwhciFhQ8E0ajfQNDh8d_UTnhUqYEEA4=; _zap=b925bd9a-6503-4eab-b6e0-d094a5d0aad6; d_c0=\"AKDf86pmhhOPTmfxy4FDwcqGKetjs62snM0=|1628217016\"; _9755xjdesxxd_=32; YD00517437729195%3AWM_TID=WbTete8TrPtBVFUUVRcqtUThEwmFLjiZ; gdxidpyhxdE=lW03QyM8AjtiphucXYo7f%5CnAaiu2hbSrC8e9%5CR9nnCVi%2FeR92T5AZL81HyImayREa8mtGRLT%5CMBoDRJe6GQg2lHBWrJS%5C1XR0M%2FuHsU6gQQJqcxGIBkAHyu5N74HxUPViXeWTMw8WRGO9aP%2BtiC044QxxVntsZJSY3nMD4ci3L2dA%2Bkz%3A1655278830180; YD00517437729195%3AWM_NI=tNYso6A%2FkdjOFzmqVHRBxqynC5SJdCnP9b0P8hqOY919keokO5G9nuBro475MBEOGrRikbKClYt7guJKc8cQRZWHMmluFUxJUdoHmcJQWOTm5sxlE7FV6lFSYqsF3emsSUs%3D; YD00517437729195%3AWM_NIKE=9ca17ae2e6ffcda170e2e6eea9ee7db896ad8fae6afb8e8fa6c14a979f9b87c85bafb8f7afd149a8bfb8b9e42af0fea7c3b92ab8929794ee3e83f1a585e94586a6a392d544929eb892fc6681eb9dabe749ae90f783b35cfcb8bfb0dc33f3e7a586ee618deb89daed7f82aea1d5b63f8f9e8bb0c67a92b7a583ca49b087bcd8e67f8a95add9e25b928699a9d067b0ab8ab6fb4297bca1a9f0698a8f99b0d767f7adb88dc83d868afa8dc5599bafbda7b26fafad9db6f237e2a3; z_c0=2|1:0|10:1655278190|4:z_c0|92:Mi4xei02TkR3QUFBQUFBb05fenFtYUdFeVlBQUFCZ0FsVk5idGlXWXdCZGlETVNIbFJGMU02ckpwT2dQbDk1TC04QjN3|b497b26a08aab2c555acb0c04c5aca234fcd1433f7dc561d03595594e22e219c; q_c1=776db9f6f90042218928df384aabd118|1655537677000|1655537677000; ariaDefaultTheme=default; _xsrf=8f2c8cf4-429e-4f1e-bf35-3d889de01188; Hm_lvt_98beee57fd2ef70ccdd5ca52b9740c49=1657955077,1658115447,1658157099,1658300990; tst=h; SESSIONID=j1YCDi9wlFtM4Nv3J2GR59SY0rED4mYiMuArNpddaAt; JOID=UlwTC0MYmlju-djpSxCGQWFatmNedqkPrbW10jJ82C6tzZ6berIrjoT13uVFbxCWmgsfWXslZfVU3Z0KKuVjxEI=; osd=VlwXAEscmlzl8dzpTxuORWFevWtadq0EpbG11jl03C6pxpaferYghoD12u5NaxCSkQMbWX8ubfFU2ZYCLuVnz0o=; Hm_lpvt_98beee57fd2ef70ccdd5ca52b9740c49=1658383068; NOT_UNREGISTER_WAITING=1; ariaStatus=true; KLBRSID=5430ad6ccb1a51f38ac194049bce5dfe|1658383081|1658383058"})
        soup = BS(resp.text, 'lxml')
        sc=json.loads(soup.find('script',{'id':"js-initialData"}).get_text())
        res={}
        res['created']=sc['initialState']['entities']['questions'][qid]['created']
        res['followerCount']=sc['initialState']['entities']['questions'][qid]['followerCount']
        res['visitCount']=sc['initialState']['entities']['questions'][qid]['visitCount']
        res['answerCount']=sc['initialState']['entities']['questions'][qid]['answerCount']
        res['title']=sc['initialState']['entities']['questions'][qid]['title']
        res['raw']=sc['initialState']['entities']['questions'][qid]['excerpt']
        res['hit_at']=time.time()
        return res
        raise NotImplementedError

if __name__ == "__main__":
    z = ZhihuCrawler()
    z.watch()
