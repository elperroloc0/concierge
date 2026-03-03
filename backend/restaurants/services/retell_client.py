from retell import Retell 
import re 

class RetellClient:
    def __init__(self, api_key: str):
        self.client = Retell(api_key=api_key)
        
    def create_retell_llm(self, **kwargs):
        """create retell llm response engin in client workspace"""
        return self.client.llm.create(**kwargs)
        
    def create_agent(self, **kwargs):
        """kwargs- will recive prompt, voice_id, language, etc. """
        return self.client.agent.create(**kwargs)
    
    def create_phone_number(self, *, area_code: int, inbound_agent_id: str, inbound_webhook_url: str = None):
        """buy a new phone number"""
        kwargs = {"area_code": area_code, "inbound_agents": [{"agent_id": inbound_agent_id, "weight": 1.0}]}
        if inbound_webhook_url:
            kwargs["inbound_webhook_url"] = inbound_webhook_url
        return self.client.phone_number.create(**kwargs)

    def update_llm(self, llm_id: str, **kwargs):
        """update an existing retell LLM (e.g. change general_prompt)"""
        return self.client.llm.update(llm_id, **kwargs)

    def update_agent(self, agent_id: str, **kwargs):
        """update an existing retell agent (e.g. change voice)"""
        return self.client.agent.update(agent_id, **kwargs)

    def update_phone_number(self, phone_number: str, **kwargs):
        """update a phone number (e.g. set inbound_webhook_url for dynamic variables)"""
        return self.client.phone_number.update(phone_number, **kwargs)
    
