# AVWikiDB 响应夹具

完整 HTTP 响应正文于 2026-11-10 使用 `curl_cffi.requests.get` 获取. 各 TOML 用例记录详情地址、请求指纹与 SHA-256.

补充响应的请求地址:

- `search-start654.html`: `https://avwikidb.com/search?q=START654`, Chrome 指纹.
- `empty.html`: `https://avwikidb.com/search/?q=AMANE-NOTFOUND-999999`, Firefox 指纹, `Referer: https://avwikidb.com/`.
- `missing.html`: `https://avwikidb.com/work/ZZZZNOTREAL-999999/`, HTTP 404, Firefox 指纹与上述 Referer. 测试先模拟 HTTP 未找到异常, 再回放空检索响应.

夹具不包含下载的图片或视频. 爬虫仅解析主影片对象; 无关推荐与评论保留在未经修改的响应正文中.
