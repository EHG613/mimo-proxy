"""PyInstaller 打包入口：client/__main__.py 使用相对导入，直接作为脚本入口
会因缺包上下文失败，这里以绝对导入建立包上下文。"""
from client.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
