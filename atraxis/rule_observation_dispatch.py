"""Collect and attest observations; decisions and workflow admission stay in Atropos."""
from .rule_observations import ObservationError


class LiveRuleDispatcher:
    def __init__(self, application, binding_loader, inventory_loader, signer):
        self.application=application
        self.binding_loader,self.inventory_loader,self.signer=binding_loader,inventory_loader,signer

    def proof(self, name, target, binding, expected_generation=None):
        # Atropos checks rule and action permissions before any telemetry read.
        challenge=self.application.observation_challenge(name,target,binding)
        if expected_generation is not None and challenge['generation']!=expected_generation:
            raise ObservationError('active_version_changed')
        mapping=self.binding_loader(tuple(target))
        if mapping.target!=tuple(target) or mapping.organization!=challenge['organization']:
            raise ObservationError('observation_binding_mismatch')
        observed=self.inventory_loader()
        payload=observed.attestation(mapping,challenge['inputs'])
        if self.binding_loader(tuple(target))!=mapping:
            raise ObservationError('observation_binding_changed')
        payload['challenge']=challenge
        return self.signer(payload),observed,mapping

    def plan(self, name, target, binding):
        proof,observed,mapping=self.proof(name,target,binding)
        result=self.application.plan_observed_dispatch(proof)
        observed.resource(mapping)  # Never display an already stale preview.
        return result

    def dispatch(self, name, target, binding, *, expected_generation, event_id):
        # Confirming always collects again; the preview is not an execution ticket.
        proof,_,_=self.proof(name,target,binding,expected_generation)
        return self.application.dispatch_observed(proof,expected_generation=expected_generation,event_id=event_id)
