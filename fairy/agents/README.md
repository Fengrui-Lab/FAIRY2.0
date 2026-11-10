agents部分负责实现fairy中所有的智能体相关内容：
- model模块：接入LLM以及Jev等模型
- loop模块：负责维护智能体的循环（现在流行称为loop engineering）
    - context模块：构建上下文
    - controller模块：控制智能体循环的运行逻辑