from json import loads, dumps
from datetime import datetime
from flask import request
import requests
import os
import itertools  # Used for round-robin cycling
import re

from server.config import special_instructions  # Restored jailbreak handling

class Backend_Api:
    def __init__(self, app, config: dict) -> None:
        self.app = app

        # List of LlamaCPP servers for load balancing
        self.llama_servers = [
            "http://belto.myftp.biz:9999/v1/chat/completions",
            "http://47.34.185.47:9999/v1/chat/completions"
        ]

        # Create a round-robin cycle iterator
        self.server_cycle = itertools.cycle(self.llama_servers)

        self.routes = {
            '/backend-api/v2/conversation': {
                'function': self._conversation,
                'methods': ['POST']
            }
        }

    def extract_links(self, text):
        """Extracts all URLs from a given text."""
        url_pattern = r"https?://[^\s]+"
        return re.findall(url_pattern, text)

    def fetch_link_metadata(self, url):
        """Sends a request to the API to fetch metadata of the given URL."""
        headers = {"API-Key": "123456789012345"}  # Replace with actual API key
        try:
            response = requests.post(
                "http://linkreader.api.beltoss.com/read_link",
                headers=headers,
                json={"url": url}
            )
            response.raise_for_status()  # Raises error if request fails
            return response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 403:
                return {"error": f"Unfortunately, I could not extract any data from that URL: {url}"}
            return {"error": str(e)}
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    def format_for_llm_combined(self, data_list):
        """Formats all extracted data into a single structured string for LLM processing."""
        combined_content = []

        for data in data_list:
            if "error" in data:
                combined_content.append(data["error"])
                continue

            url = data.get("url", "Unknown URL")
            content = data.get("content", "").strip()
            title = content.splitlines()[0] if content else None
            likes = content.split("Likes: ")[-1].split()[0] if "Likes: " in content else None
            views = content.split("Views: ")[-1].split()[0] if "Views: " in content else None
            summary = data.get("summary", None)

            formatted_entry = f"URL: {url}"
            if title:
                formatted_entry += f"\nTitle: {title}"
            if likes and likes != "N/A":
                formatted_entry += f"\nLikes: {likes}"
            if views and views != "N/A":
                formatted_entry += f"\nViews: {views}"
            if summary:
                formatted_entry += f"\nSummary: {summary}"

            # Include full content with a reasonable limit
            if content:
                max_length = 3000  # Limit to avoid token overflow
                truncated_content = content[:max_length] + ("..." if len(content) > max_length else "")
                formatted_entry += f"\n\nExtracted Content:\n{truncated_content}"

            combined_content.append(formatted_entry)

        return "\n\n---\n\n".join(combined_content)  # Separator between entries

    def _conversation(self):
        try:
            # Extract request parameters
            jailbreak = request.json.get('jailbreak', 'default')
            _conversation = request.json['meta']['content']['conversation']
            prompt = request.json['meta']['content']['parts'][0]
            current_date = datetime.now().strftime("%Y-%m-%d")
            internet_access = request.json['meta']['content']['internet_access']

            extracted_content = ""

            if internet_access:
                internet_query = prompt["content"]
                print(f"Internet Access Query: {internet_query}")

                # Extract URLs from the user's query
                extracted_links = self.extract_links(internet_query)

                if extracted_links:
                    data_list = []
                    for link in extracted_links:
                        print(f"Fetching content from: {link}")
                        metadata = self.fetch_link_metadata(link)
                        data_list.append(metadata)

                    # Format the extracted content
                    extracted_content = self.format_for_llm_combined(data_list)
                    print(f"Extracted Web Content:\n{extracted_content}")

                    # Append extracted content to the user's prompt
                    prompt["content"] += f"\n\n[Extracted Web Content]\n{extracted_content}"

            # Construct system message with current date
            system_message = {
                "role": "system",
                "content": f'You are BeltoAI, a large language model implemented by experts with Belto. Strictly follow the users instructions. Current date: {current_date}'
            }

            # Construct conversation with jailbreak instructions
            conversation = [system_message] + special_instructions.get(jailbreak, []) + _conversation + [prompt]

            # Select the next LlamaCPP server using round-robin
            selected_server = next(self.server_cycle)
            print(selected_server)

            # Prepare the request payload
            payload = {
                "model": request.json.get("model", "gpt-3.5-turbo"),  # Default model
                "messages": conversation,
                "stream": True  # Streaming enabled for continuous response
            }

            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer qQhUOBjNamjELp2g69ww8APeFD8FNHW8"  # Shared API key
            }

            # Send request to the selected LlamaCPP server
            print(f"Sending request to LlamaCPP server: {selected_server}")
            print(f"Payload: {dumps(payload, indent=2)}")

            llama_resp = requests.post(selected_server, headers=headers, json=payload, stream=True)

            # Log status code for debugging
            print(f"Response Status Code: {llama_resp.status_code}")

            if llama_resp.status_code >= 400:
                print(f"Error Response: {llama_resp.text}")
                return {
                    "success": False,
                    "error_code": llama_resp.status_code,
                    "message": llama_resp.text
                }, llama_resp.status_code

            # Streaming the response back to the client
            def stream():
                for chunk in llama_resp.iter_lines():
                    if not chunk:
                        continue  # Skip empty lines

                    decoded_chunk = chunk.decode("utf-8").strip()

                    # Ignore "[DONE]" signal
                    if decoded_chunk == "data: [DONE]":
                        break

                    # Ensure we only parse valid JSON chunks
                    if decoded_chunk.startswith("data: "):
                        try:
                            json_data = loads(decoded_chunk[6:])  # Remove "data: " prefix
                            token = json_data["choices"][0]["delta"].get("content", "")
                            if token:
                                yield token
                        except Exception as e:
                            print(f"Error parsing chunk: {e}")
                            continue  # Skip invalid chunks

            return self.app.response_class(stream(), mimetype="text/event-stream")

        except Exception as e:
            print(f"Exception: {e}")
            return {
                "success": False,
                "error": f"An error occurred: {str(e)}"
            }, 400
