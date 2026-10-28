"""Capa de interpretación contextual.

Responsabilidad: entender lenguaje cotidiano y PROPONER (atributos, candidatos, motivos de
incertidumbre). No decide nada: el motor determinista (`domain/` + `services/`) valida, y solo
una persona autorizada o un alias verificado confirma un atributo. La IA, cuando exista, vive
detrás de la interfaz `AIInterpreter` y sus propuestas se degradan a candidatos no verificados.
"""
