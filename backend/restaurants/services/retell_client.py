from retell import Retell 

class RetellClient:
    def __init__(self, api_key: str):
        self.client = Retell(api_key=api_key)
        
    def create_retell_llm(self, **kwargs):
        """create retell llm response engin in client workspace"""
        return self.client.llm.create(**kwargs)
        
    def create_agent(self, **kwargs):
        """kwargs- will recive prompt, voice_id, language, etc. """
        return self.client.agent.create(**kwargs)