fix(delivery): 既有PR的冪等metadata PATCH/PUT/reread只對HTTP 502/503/504做finite retry，不擴張至create、push或merge side effect。
